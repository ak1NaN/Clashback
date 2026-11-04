"""GATE 2d: GameMain::init runs, with the CR sandbox's binderless recipe (jni_bridge.cpp nativeInitGameMain, default switches):
every patch site's original bytes are checked first (a different engine build is refused, never patched blindly); config
registrations, config getters and the config postprocess call are bypassed and the five renderer calls skipped (graphics and
Android services only, no battle or data-table code); GameMain::init(game, context) runs; every original byte is restored; then
the skip-core-and-presentation flag is set and the headless resource-variant function made to return an empty variant.

    python3 tools/crx/gate2d.py LIBDIR WORKDIR ASSETS
"""
import ctypes
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gate2b as B  # noqa: E402
import gate2c as C  # noqa: E402
import jni as J  # noqa: E402
import bindings  # noqa: E402
import loader as L  # noqa: E402
import patch as P  # noqa: E402

# Bypassed only for the length of GameMain::init (all graphics / client-config paths; restored right after). Addresses: bindings.
TEMPORARY = [
    P.ret('config_string_registration'),                       # config string registration -> return
    P.ret('config_flag_registration'),                         # config flag registration -> return
    P.ret0('toml_getter_a', 2), P.ret0('toml_getter_b', 2),     # two config getters -> return 0
    P.ret0('json_get_object', 2),                               # (the same getter the JSON code uses; restored after init)
    # NOT patched (the sandbox recipe did, and that broke the data): the TOML getters toml_has_key / toml_get_value and the
    # manifest_loop_call, which is the DATA MANIFEST loop, not a config step -- it registers the per-object TOML files
    # (csv_logic/characters/*.toml ...) that hold, e.g., every card's hitpoints and damage. Skipping it left those rows empty and
    # the reference-linking stage crashed on a null link (gate 2e, 2026-10-07).
    P.nop('renderer_bootstrap_call'),                           # renderer bootstrap call
    P.jump('renderer_device_block', 'renderer_device_block_end', 2),     # renderer device block -> jump over
    P.nop('renderer_resolution_call'),                          # renderer resolution call
    P.jump('renderer_objects_block', 'renderer_objects_block_end', 1),   # renderer objects block -> jump over
    P.nop('renderer_late_configure_call'),                      # renderer late configure call
]
# CRX_UNPATCHED=name,...: leave these places alone during GameMain::init (experiments: which bypass is really needed).
import os  # noqa: E402
_UNPATCHED = {x for x in os.environ.get('CRX_UNPATCHED', '').split(',') if x}
TEMPORARY = [t for t in TEMPORARY if t.place not in _UNPATCHED]
VARIANT_EMPTY = P.Patch('resource_variant', 5, [('raw', '0f57c00f11074889f8c3')], stub=False)   # xorps; movups [rdi]; mov rax,rdi; ret


_INIT_DONE = []         # GameMain::init runs once per process (the helper field the sandbox checked is gone in newer builds)


def game_main_init(g) -> dict:
    B = bindings.of(g)
    game = C.g64(g, B.glob('game_singleton_global'))
    context = ctypes.c_uint64.from_address(game + B.field('game.context')).value if game else 0
    helper_before = ctypes.c_uint64.from_address(game + B.field('game.helper')).value if game else 0
    if not game or not context:
        raise RuntimeError('GameMain/context is not ready')
    wrong = B.check([t.place for t in TEMPORARY] + ['resource_variant', 'game_main_init'])
    if wrong:
        raise RuntimeError('patch guard rejected the engine bytes: {} (a different build?)'.format(wrong))
    if not _INIT_DONE:
        _INIT_DONE.append(True)
        records = []
        try:
            for t in TEMPORARY:
                records.append(t.apply(g, B))
            g.call(B.fn('game_main_init'), game, context)
        finally:
            for record in reversed(records):
                P.undo(record)
    restored = not B.check([t.place for t in TEMPORARY])
    flag = B.location('skip_core_and_presentation_flag')
    if flag:
        ctypes.c_uint8.from_address(flag).value = 1
    out16, empty8 = (ctypes.c_uint8 * 16)(), (ctypes.c_uint8 * 8)()
    g.call(B.fn('resource_variant'), ctypes.addressof(out16), 0, ctypes.addressof(empty8))
    if not os.environ.get('CRX_KEEP_RESOURCE_VARIANT'):         # render/: the game's own renderer needs its graphics resources
        VARIANT_EMPTY.apply(g, B)
    return {'game': game, 'context': context, 'helper_before': helper_before,
            'helper_after': ctypes.c_uint64.from_address(game + B.field('game.helper')).value,
            'manager': C.g64(g, B.glob('manager_global')), 'resource_loader': C.g64(g, B.glob('resource_loader_global')),
            'originals_restored': restored}


def main() -> int:
    libdir, work, assets = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    for name in ('data', 'cache', 'external'):
        (work / name).mkdir(parents=True, exist_ok=True)
    L.install_crash_report()
    ld, g, vm = B.start(libdir, work)
    vm.answers['com/supercell/titan/ApplicationUtilBase.getAppVersion()Ljava/lang/String;'] = '15.535.29'
    store = {}                                         # Android's key-value preferences, kept in memory

    def text(handle):
        kind, value = vm.objects.get(handle or 0, ('null', None))
        return value if kind == 'string' else None

    def store_kv(args):
        key, value = J.va_args(args[3], 2)
        store[text(key)] = text(value)
        return 0
    vm.answers['com/supercell/titan/ApplicationUtilBase.storeKeyValue(Ljava/lang/String;Ljava/lang/String;)V'] = store_kv
    vm.answers['com/supercell/titan/ApplicationUtilBase.getKeyValue(Ljava/lang/String;)Ljava/lang/String;'] = (
        lambda args: store.get(text(J.va_args(args[3], 1)[0])))
    B.call_native(vm, 'com/supercell/titan/TitanApplication.nOnNativeLibrariesLoaded')
    B.call_native(vm, 'com/supercell/titan/GameApp.nOnApplicationCreate')
    B.call_native(vm, 'com/supercell/titan/GameApp.createGameMain', vm.new('android/content/res/AssetManager', 'assets'),
                  vm.string(str(work / 'data')), vm.string(str(work / 'cache')), vm.string(str(work / 'external')),
                  ctypes.c_long(8 << 30), ctypes.c_int(1080), ctypes.c_int(2400), ctypes.c_int(420), ctypes.c_float(420.0),
                  ctypes.c_float(420.0), ctypes.c_int(2), vm.string(str(work / 'external')), vm.new('com/supercell/titan/GameApp', 'activity'))
    seen = len(J.LOG)
    B.call_native(vm, 'com/supercell/titan/GameApp.nOnCreate')           # probe-direct: activity create, no surface
    print('  JNI during nOnCreate:', [f'{n} {d}' for n, d in J.LOG[seen:] if n not in ('DeleteLocalRef', 'ExceptionCheck')][:20])
    import time
    game = C.g64(g, C.R['game_singleton_global'])
    start = time.time()
    while time.time() - start < 5.0:                                       # the sandbox's headless hold: engine threads run
        if game and ctypes.c_uint64.from_address(game + 8).value:
            break
        time.sleep(0.1)
    print(f'  after the hold ({time.time() - start:.1f} s): game 0x{game:x}, context 0x{ctypes.c_uint64.from_address(game + 8).value if game else 0:x}')
    C.init_resources(g, assets)
    seen = len(J.LOG)
    out = game_main_init(g)
    for k, v in out.items():
        print(f'  {k}: {hex(v) if isinstance(v, int) and not isinstance(v, bool) else v}')
    asked = [f'{n} {d}' for n, d in J.LOG[seen:] if n not in ('DeleteLocalRef', 'ExceptionCheck', 'NewGlobalRef', 'DeleteGlobalRef')]
    print(f'  JNI during GameMain::init: {len(asked)} calls', asked[:25])
    print(f'  key-value store: {store}')
    odd = [a for a in L.ASSET_LOG if not a.replace('   (missing)', '').isprintable()]
    print(f'  assets read: {len(L.ASSET_LOG)}, with unreadable characters in the path: {len(odd)} {odd[:3]}')
    ok = out['helper_after'] and out['originals_restored']
    print('GATE 2d PASSED' if ok else 'GATE 2d FAILED')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
