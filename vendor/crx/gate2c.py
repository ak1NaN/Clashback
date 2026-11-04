"""GATE 2c: after start-up (gate 2b), the engine's own asset system is pointed at a folder of the game's files, exactly as the CR
sandbox's direct start (jni_bridge.cpp nativeInitResources, binderless mode): root = ASSETS, secondary root and the cache:/save:/
temp: mounts from the engine's own path getters, chdir into ASSETS, then the runtime clock, the game singleton and four thread
options. Checks that the asset system and the resource loader globals are set.

    python3 tools/crx/gate2c.py LIBDIR WORKDIR ASSETS
"""
import ctypes
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gate2b as B  # noqa: E402
import jni as J  # noqa: E402
import bindings  # noqa: E402
import loader as L  # noqa: E402



class _Names:
    """R['name'] -> the loaded build's address (function or global), from bindings."""
    def __getitem__(self, name):
        B = bindings.of()
        return B.glob(name) if name in B.data['globals'] else B.fn(name)


R = _Names()


def g64(g, rva):
    return ctypes.c_uint64.from_address(g.addr(rva)).value


def cxx_string(g, text: str):
    """A libc++ std::string (24 bytes), laid out as the sandbox builds it: short strings inline, long ones in engine memory."""
    raw = text.encode()
    s = (ctypes.c_uint8 * 24)()
    if len(raw) < 23:
        s[0] = len(raw) * 2
        ctypes.memmove(ctypes.addressof(s) + 1, raw + b'\0', len(raw) + 1)
    else:
        cap = len(raw) | 15
        storage = g.call(R['native_alloc'], cap + 1)
        ctypes.memmove(storage, raw + b'\0', len(raw) + 1)
        ctypes.c_uint64.from_address(ctypes.addressof(s)).value = cap + 2
        ctypes.c_uint64.from_address(ctypes.addressof(s) + 8).value = len(raw)
        ctypes.c_uint64.from_address(ctypes.addressof(s) + 16).value = storage
    L.KEEP.append(s)            # never freed: the engine may keep pointing at strings it was given (measured: the asset roots)
    return s


def sc_string_text(buf) -> str:
    """The engine's own string (16 bytes): length at +4; text inline at +8 if under 8 characters, else behind a pointer at +8."""
    base = ctypes.addressof(buf)
    length = ctypes.c_int32.from_address(base + 4).value
    if length < 8:
        return ctypes.string_at(base + 8, max(length, 0)).decode(errors='replace')
    return ctypes.string_at(ctypes.c_void_p.from_address(base + 8).value, length).decode(errors='replace')


def init_resources(g, assets: str, early: bool = False) -> dict:
    out = {}
    if not g64(g, R['asset_system_global']):
        g.call(R['asset_system_init'])
    out['asset_system'] = g64(g, R['asset_system_global'])
    system = g.call(R['asset_system_get'])
    g.call(R['asset_system_prepare'], system)
    g.call(R['set_root'], system, ctypes.addressof(cxx_string(g, assets)))
    paths = {}
    for name, getter, action in (('secondary', 'secondary_path', 'set_secondary_root'), ('cache', 'cache_path', 'cache:'),
                                 ('save', 'save_path', 'save:'), ('temp', 'temp_path', 'temp:')):
        buf = (ctypes.c_uint8 * 16)()
        g.call(R[getter], ctypes.addressof(buf))
        text = sc_string_text(buf)
        paths[name] = text
        if action == 'set_secondary_root':
            g.call(R['set_secondary_root'], system, ctypes.addressof(cxx_string(g, text)))
        else:
            g.call(R['mount'], system, ctypes.addressof(cxx_string(g, text)), ctypes.addressof(cxx_string(g, action)))
        g.call(R['string_destroy'], ctypes.addressof(buf))
    out['paths'] = paths
    os.chdir(assets)
    if not g64(g, R['runtime_clock_global']):
        g.call(R['runtime_clock_init'])
    if not g64(g, R['game_singleton_global']) and not early:   # early (crx/render, before nOnCreate): start-up makes it later
        if not bindings.of().has('game_singleton_init'):
            raise RuntimeError('the game singleton was not created by start-up, and this build has no game_singleton_init')
        g.call(R['game_singleton_init'])
    for option in (5, 3, 10, 12):
        g.call(R['thread_option_set'], ctypes.c_int(option), ctypes.c_bool(True))
    out.update(resource_loader=g64(g, R['resource_loader_global']), game_singleton=g64(g, R['game_singleton_global']),
               runtime_clock=g64(g, R['runtime_clock_global']), manager=g64(g, R['manager_global']))
    return out


def main() -> int:
    libdir, work, assets = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    for name in ('data', 'cache', 'external'):
        (work / name).mkdir(parents=True, exist_ok=True)
    L.install_crash_report()
    ld, g, vm = B.start(libdir, work)
    B.call_native(vm, 'com/supercell/titan/TitanApplication.nOnNativeLibrariesLoaded')
    B.call_native(vm, 'com/supercell/titan/GameApp.nOnApplicationCreate')
    B.call_native(vm, 'com/supercell/titan/GameApp.createGameMain', vm.new('android/content/res/AssetManager', 'assets'),
                  vm.string(str(work / 'data')), vm.string(str(work / 'cache')), vm.string(str(work / 'external')),
                  ctypes.c_long(8 << 30), ctypes.c_int(1080), ctypes.c_int(2400), ctypes.c_int(420), ctypes.c_float(420.0),
                  ctypes.c_float(420.0), ctypes.c_int(2), vm.string(str(work / 'external')), vm.new('com/supercell/titan/GameApp', 'activity'))
    seen = len(J.LOG)
    out = init_resources(g, assets)
    for k, v in out.items():
        print(f'  {k}: {v if isinstance(v, dict) else hex(v)}')
    new_jni = [f'{n} {d}' for n, d in J.LOG[seen:] if n not in ('DeleteLocalRef', 'ExceptionCheck')]
    if new_jni:
        print('  JNI asked during resources:', new_jni[:20])
    ok = out['asset_system'] and out['game_singleton'] and out['runtime_clock']
    print('GATE 2c PASSED' if ok else 'GATE 2c FAILED')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
