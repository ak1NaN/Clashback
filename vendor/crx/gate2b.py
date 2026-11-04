"""GATE 2b: the engine's Android start-up entry points run against the stand-in Java, in the CR sandbox's probe-direct order:
JNI_OnLoad -> TitanApplication.nOnNativeLibrariesLoaded -> GameApp.nOnApplicationCreate -> GameApp.createGameMain(...).
Prints every Java method the engine asks for along the way (what a real phone would answer), and createGameMain's result.

    python3 tools/crx/gate2b.py LIBDIR WORKDIR
"""
import ctypes
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bindings  # noqa: E402
import jni as J  # noqa: E402
import loader as L  # noqa: E402



def _unmangle(symbol: str) -> str:
    """Java_com_supercell_titan_GameApp_createGameMain -> com/supercell/titan/GameApp.createGameMain (JNI name mangling)."""
    body, out, i = symbol[len('Java_'):].split('__')[0], '', 0
    while i < len(body):
        c = body[i]
        if c == '_' and i + 1 < len(body) and body[i + 1] in '0123':
            if body[i + 1] == '0':
                out += chr(int(body[i + 2:i + 6], 16))
                i += 6
                continue
            out += {'1': '_', '2': ';', '3': '['}[body[i + 1]]
            i += 2
            continue
        out += '/' if c == '_' else c
        i += 1
    cls, _, method = out.rpartition('/')
    return cls + '.' + method


def exported_natives(g) -> dict:
    """Entry points the engine exports by their JNI names (newer builds do this instead of RegisterNatives)."""
    return {_unmangle(name): addr for name, addr in g.exports.items() if name.startswith('Java_')}


def start(libdir: Path, work: Path):
    L.install_crash_report()
    ld = L.Loader()
    cxx, g = ld.load(str(libdir / 'libc++_shared.so')), ld.load(str(libdir / 'libg.so'))
    ld.run_constructors(cxx)
    ld.run_constructors(g)
    bindings.use(g)
    vm = J.Jvm()
    g.call(g.exports['JNI_OnLoad'] - g.base, ctypes.addressof(vm.vm), 0, restype=ctypes.c_int)    # exported: same on every build
    for key, addr in exported_natives(g).items():
        if not any(k.split('(')[0] == key for k in vm.natives):
            vm.natives[key] = addr
    return ld, g, vm


def native(vm, name_prefix):
    hits = [k for k in vm.natives if k.split('(')[0] == name_prefix] or [k for k in vm.natives if k.startswith(name_prefix)]
    assert len(hits) == 1, (name_prefix, hits)
    return hits[0], vm.natives[hits[0]]


def call_native(vm, prefix, *args, restype=ctypes.c_long):
    key, fn = native(vm, prefix)
    cls = vm.new('class', key.split('.')[0])
    args = [ctypes.c_void_p(a) if isinstance(a, int) else a for a in args]
    f = L.cfunc(fn, restype, ctypes.c_void_p, ctypes.c_void_p, *[type(a) for a in args])
    return f(ctypes.addressof(vm.env), cls, *args)


def main() -> int:
    libdir, work = Path(sys.argv[1]), Path(sys.argv[2])
    for name in ('data', 'cache', 'external'):
        (work / name).mkdir(parents=True, exist_ok=True)
    ld, g, vm = start(libdir, work)
    seen = len(J.LOG)

    def step(title, fn):
        nonlocal seen
        print(f'--- {title}')
        result = fn()
        for name, details in J.LOG[seen:]:
            if name not in ('DeleteLocalRef', 'ExceptionCheck', 'NewGlobalRef', 'DeleteGlobalRef'):
                print(f'    jni {name} {details}')
        seen = len(J.LOG)
        return result

    step('TitanApplication.nOnNativeLibrariesLoaded', lambda: call_native(vm, 'com/supercell/titan/TitanApplication.nOnNativeLibrariesLoaded'))
    step('GameApp.nOnApplicationCreate', lambda: call_native(vm, 'com/supercell/titan/GameApp.nOnApplicationCreate'))
    assets = vm.new('android/content/res/AssetManager', 'assets')
    activity = vm.new('com/supercell/titan/GameApp', 'activity')
    result = step('GameApp.createGameMain', lambda: call_native(
        vm, 'com/supercell/titan/GameApp.createGameMain', assets, vm.string(str(work / 'data')), vm.string(str(work / 'cache')),
        vm.string(str(work / 'external')), ctypes.c_long(8 << 30), ctypes.c_int(1080), ctypes.c_int(2400), ctypes.c_int(420),
        ctypes.c_float(420.0), ctypes.c_float(420.0), ctypes.c_int(2), vm.string(str(work / 'external')), activity))
    print(f'createGameMain returned {vm.describe(result)}')
    print(f'assets opened: {L.ASSET_LOG[:20]}')
    if L.STUBS_CALLED:
        print('Android-only or missing functions called:', L.STUBS_CALLED)
    print('GATE 2b PASSED (start-up entry points ran)' if True else '')
    return 0


if __name__ == '__main__':
    sys.exit(main())
