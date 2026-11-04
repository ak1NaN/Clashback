"""Load Android x86_64 libraries (the game engine libg.so and its C++ runtime libc++_shared.so) into a plain Linux process, with
Python's standard library only: no root, no Android, no emulator, nothing installed.

How: map each library's segments into memory (mmap), apply its relocations, and connect every function it needs from Android's
C library to the node's own C library (glibc). On x86_64 Android and Linux use the same calling convention, so most functions
connect directly; the few that differ get a small adapter (SHIMS below). Then run the libraries' constructors.

    loader = Loader(); loader.load('libc++_shared.so'); g = loader.load('libg.so'); g.call(0x1234, ...)
"""
import ctypes
import ctypes.util
import os
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from elf import DT, PT_LOAD, Elf  # noqa: E402

PROT_READ, PROT_WRITE, PROT_EXEC = 1, 2, 4
MAP_PRIVATE, MAP_ANONYMOUS = 0x02, 0x20
R_X86_64_64, R_X86_64_GLOB_DAT, R_X86_64_JUMP_SLOT, R_X86_64_RELATIVE = 1, 6, 7, 8
# per ELF machine: (absolute 64-bit, GOT entry, PLT entry, base-relative). 62 = x86_64; 183 = AArch64 (Brawl Stars' arm64
# engine, tools/bsx: Android and Linux share the AArch64 calling convention too, variadic functions included)
RELOCATIONS = {62: (1, 6, 7, 8), 183: (257, 1025, 1026, 1027)}
import platform  # noqa: E402
ARM64 = platform.machine() in ('aarch64', 'arm64')

# Windows: the engine's Android C library is tools/crx/win/bionic_win.dll and calls cross the calling-convention boundary
# there (plat_win.py). Linux: the node's own glibc, called directly (same convention as Android).
WINDOWS = sys.platform == 'win32'
# macOS (an x86_64 process under Rosetta, crx/mac): the same convention as Android; the C library parts macOS does differently
# come from crx/mac/bionic_mac.dylib (plat_mac.py).
MAC = sys.platform == 'darwin'
if WINDOWS:
    import plat_win as PW
    libc = libm = libdl = None
elif MAC:
    import plat_mac as PM
    libc = libm = libdl = PM.libc
    MAP_ANONYMOUS = PM.MAP_ANON
    libdl.dladdr.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
else:
    libc = ctypes.CDLL(ctypes.util.find_library('c') or 'libc.so.6', use_errno=True)
    libm = ctypes.CDLL(ctypes.util.find_library('m') or 'libm.so.6')
    libdl = ctypes.CDLL(None)
    libc.mmap.restype = ctypes.c_void_p
    libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_long]
    libc.mprotect.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
    libdl.dlsym.restype = ctypes.c_void_p
    libdl.dlsym.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
RTLD_DEFAULT = ctypes.c_void_p(0)

KEEP = []                                   # Python objects the native code points into (callbacks, buffers): never freed


def native(name: str) -> 'int | None':
    """The address of a function in the node's C library (libc, libm, libdl, ...; on Windows bionic_win.dll), or None."""
    if WINDOWS:
        return PW.native(name)
    if MAC:
        return PM.native(name)
    return libdl.dlsym(RTLD_DEFAULT, name.encode()) or None


def callback(restype, *argtypes):
    """Decorator: an address the engine can call (with its own calling convention) that runs the Python function."""
    if WINDOWS:
        return PW.callback(restype, *argtypes)

    def wrap(fn):
        cb = ctypes.CFUNCTYPE(restype, *argtypes)(fn)
        KEEP.append(cb)
        return ctypes.cast(cb, ctypes.c_void_p).value
    return wrap


def cfunc(addr: int, restype, *argtypes):
    """A Python callable for engine code at addr (ctypes.CFUNCTYPE(restype, *argtypes)(addr) on Linux)."""
    if WINDOWS:
        return PW.cfunc(addr, restype, *argtypes)
    return ctypes.CFUNCTYPE(restype, *argtypes)(addr)


def alloc_exec(size: int, hint: 'int | None' = None) -> 'int | None':
    """Executable, writable, zeroed memory (at hint if free there, else anywhere; None on failure)."""
    if WINDOWS:
        return PW.alloc_exec(size, hint) or (PW.alloc_exec(size) if hint is None else None)
    if MAC:
        return PM.alloc_exec(size, hint)
    mem = libc.mmap(hint, size, PROT_READ | PROT_WRITE | PROT_EXEC, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0)
    return None if mem in (None, ctypes.c_void_p(-1).value) else mem


def free_exec(addr: int, size: int) -> None:
    if WINDOWS:
        PW.free_exec(addr, size)
    else:
        libc.munmap(ctypes.c_void_p(addr), size)


# ----------------------------------------------------------------------------------------------------------------------------
# Adapters: what Android's C library has that the node's does not, or has differently.
SHIMS: dict = {}
STUBS_CALLED: dict = {}           # Android-only functions that did nothing: counted, so nothing is hidden


def shim(name, restype, *argtypes):
    def deco(fn):
        SHIMS[name] = callback(restype, *argtypes)(fn)
        return fn
    return deco


def stub(name: str, value: int = 0):
    """An Android-only function the simulation does not need (sound, screen, accounts, crash reports): returns `value`."""
    def fn(*_args):
        STUBS_CALLED[name] = STUBS_CALLED.get(name, 0) + 1
        return value
    SHIMS[name] = callback(ctypes.c_long)(fn)


SHIMS['__errno'] = None if MAC else native('__errno_location')      # same job, different name (macOS: bionic_mac's)


@shim('__system_property_get', ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p)
def _prop(name, value):
    ctypes.memset(value, 0, 1)
    return 0


@shim('__android_log_write', ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_char_p)
def _log_write(prio, tag, text):
    if os.environ.get('CRX_LOG'):
        sys.stderr.write(f'[{(tag or b"").decode(errors="replace")}] {(text or b"").decode(errors="replace")}\n')
    return 0


SHIMS['__android_log_print'] = native('printf') if os.environ.get('CRX_LOG') else None    # variadic: print or ignore
if SHIMS['__android_log_print'] is None:
    stub('__android_log_print')
stub('android_set_abort_message')
for _hook in ('ZSTD_trace_compress_begin', 'ZSTD_trace_compress_end', 'ZSTD_trace_decompress_begin', 'ZSTD_trace_decompress_end'):
    stub(_hook)                                              # zstd's optional tracing hooks: 0 = no tracing
stub('__android_log_assert')


# glibc before 2.33 has no stat/fstat/lstat symbols, only __xstat/__fxstat/__lxstat(version, ...). Without this the engine got a
# stub that "succeeded" without filling the struct (gate 2e crashed on glibc 2.31). The adapter is machine code: no Python on the
# path. x86_64 glibc's struct stat (version 1) is the kernel's, the same layout Android's bionic uses.
def _native_code(code: bytes) -> int:
    mem = libc.mmap(None, 4096, PROT_READ | PROT_WRITE | PROT_EXEC, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0)
    ctypes.memmove(mem, code, len(code))
    return mem


for _name, _old in (('stat', '__xstat'), ('fstat', '__fxstat'), ('lstat', '__lxstat')):
    if not ARM64 and not MAC and native(_name) is None and native(_old) is not None:
        SHIMS[_name] = _native_code(bytes.fromhex('4889f2' '4889fe' 'bf01000000')    # rdx=buf; rsi=path/fd; edi=1 (version)
                                    + b'\xff\x25\x00\x00\x00\x00' + struct.pack('<Q', native(_old)))   # jmp __xstat...


def real_function(name: str) -> int:
    """The address the engine gets for a C library function: the adapter when there is one, else the node's own."""
    return SHIMS.get(name) or native(name)


# CRX_TRACE_FILES: print every path the engine hands the plain file functions (outside the asset manager), then do the real call.
if os.environ.get('CRX_TRACE_FILES') and not WINDOWS and not MAC:
    def _traced(name, restype, *argtypes):
        real = ctypes.CFUNCTYPE(restype, *argtypes)(real_function(name))
        KEEP.append(real)

        def fn(path, *rest):
            result = real(path, *rest)
            sys.stderr.write(f'crx: {name}({(path or b"").decode(errors="replace")!r}) -> {result}\n')
            return result
        SHIMS[name] = callback(restype, *argtypes)(fn)
    for _name, _sig in (('fopen', (ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p)),
                        ('open', (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_int)),
                        ('opendir', (ctypes.c_void_p, ctypes.c_char_p)),
                        ('stat', (ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p)),
                        ('lstat', (ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p)),
                        ('access', (ctypes.c_int, ctypes.c_char_p, ctypes.c_int))):
        _traced(_name, *_sig)


# CRX_TRAP_HUGE_ALLOC: an allocation of more than 1 TB is a bug somewhere (a pointer passed as a size); stop right there with a
# native crash report (SIGILL; stack, registers -- the size is in the register named below) instead of letting the allocator
# fail later. The check is a few machine instructions in front of the real function: no Python on the allocation path.
# Debugging only.
if os.environ.get('CRX_TRAP_HUGE_ALLOC') and not ARM64 and not WINDOWS and not MAC:
    _CMP = {'rdi': '4839c7', 'rsi': '4839c6', 'rdx': '4839c2'}

    def _trap_alloc(name, size_register):
        code = (b'\x48\xb8' + struct.pack('<Q', 1 << 40)          # mov rax, 1 TB
                + bytes.fromhex(_CMP[size_register] + '7602' '0f0b')  # cmp size, rax; jbe +2; ud2
                + b'\xff\x25\x00\x00\x00\x00' + struct.pack('<Q', native(name)))   # jmp real
        mem = libc.mmap(None, 4096, PROT_READ | PROT_WRITE | PROT_EXEC, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0)
        ctypes.memmove(mem, code, len(code))
        SHIMS[name] = mem
    for _name, _reg in (('malloc', 'rdi'), ('calloc', 'rsi'), ('realloc', 'rsi'), ('posix_memalign', 'rdx'), ('mmap', 'rsi'),
                        ('mremap', 'rdx')):
        _trap_alloc(_name, _reg)


# Sentry (the game's crash reporter): nothing is sent anywhere, but the text the engine hands it -- breadcrumbs and messages
# logged just before trouble -- is printed when CRX_SENTRY is set, so the engine can say what it was doing.
def _sentry_text(kind):
    def fn(*texts):
        if os.environ.get('CRX_SENTRY'):
            shown = ' | '.join(t.decode(errors='replace') for t in texts if t)
            sys.stderr.write(f'[sentry {kind}] {shown}\n')
        STUBS_CALLED['sentry_' + kind] = STUBS_CALLED.get('sentry_' + kind, 0) + 1
        return 0
    return fn


SHIMS['sentry_value_new_string'] = callback(ctypes.c_long, ctypes.c_char_p)(_sentry_text('string'))
SHIMS['sentry_value_new_breadcrumb'] = callback(ctypes.c_long, ctypes.c_char_p, ctypes.c_char_p)(_sentry_text('breadcrumb'))
SHIMS['sentry_value_new_message_event'] = callback(ctypes.c_long, ctypes.c_int, ctypes.c_char_p, ctypes.c_char_p)(
    lambda _level, logger, text: _sentry_text('message')(logger, text))
SHIMS['sentry_set_tag'] = callback(ctypes.c_long, ctypes.c_char_p, ctypes.c_char_p)(_sentry_text('tag'))
SHIMS['sentry_set_extra'] = callback(ctypes.c_long, ctypes.c_char_p, ctypes.c_long)(lambda key, _v: _sentry_text('extra')(key))
for _name in ('sentry_value_set_by_key', 'sentry_add_breadcrumb', 'sentry_capture_event'):
    stub(_name)

if not WINDOWS and not MAC:          # on Windows bionic_win.dll provides these (tools/crx/win); on macOS bionic_mac.dylib
    # Android's own "checked" variants of string functions (fortify) that glibc does not have
    SHIMS['__strlen_chk'] = native('strlen')
    SHIMS['__strchr_chk'] = native('strchr')
    SHIMS['__strrchr_chk'] = native('strrchr')


    @shim('__strncpy_chk2', ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_size_t, ctypes.c_size_t)
    def _strncpy_chk2(dst, src, n, dst_len, src_len):
        return libc.strncpy(ctypes.c_void_p(dst), ctypes.c_void_p(src), ctypes.c_size_t(n))


    # sysconf: the setting numbers differ between Android and glibc
    _SC_ANDROID_TO_GLIBC = {0x27: 30, 0x28: 30, 0x60: 83, 0x61: 84, 0x62: 85, 0x63: 86, 0x2: 2, 0x6: 4}   # page size, cpus, clock ticks, open max
    libc.sysconf.argtypes = [ctypes.c_int]
    libc.sysconf.restype = ctypes.c_long


    @shim('sysconf', ctypes.c_long, ctypes.c_int)
    def _sysconf(name):
        return libc.sysconf(_SC_ANDROID_TO_GLIBC.get(name, name))


    # Android's stdio files: __sF is an array of three FILE structures (stdin, stdout, stderr). The engine only passes their
    # addresses back to stdio functions, so a placeholder array is enough, and the stdio functions that may receive one are wrapped
    # to swap in the node's real stdout/stderr.
    _SF = ctypes.create_string_buffer(3 * 152)
    KEEP.append(_SF)
    SF_ADDR = ctypes.addressof(_SF)
    SHIMS['__sF'] = SF_ADDR
    _real_std = {SF_ADDR + 152: ctypes.c_void_p.in_dll(libc, 'stdout').value, SF_ADDR + 304: ctypes.c_void_p.in_dll(libc, 'stderr').value,
                 SF_ADDR: ctypes.c_void_p.in_dll(libc, 'stdin').value}
    SHIMS['stderr'] = ctypes.addressof(ctypes.c_void_p.in_dll(libc, 'stderr'))
    libc.fflush.argtypes = [ctypes.c_void_p]
    libc.fwrite.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_size_t, ctypes.c_void_p]
    libc.fwrite.restype = ctypes.c_size_t
    libc.fputs.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    libc.fputc.argtypes = [ctypes.c_int, ctypes.c_void_p]


    @shim('fflush', ctypes.c_int, ctypes.c_void_p)
    def _fflush(f):
        return libc.fflush(ctypes.c_void_p(_real_std.get(f or 0, f)))


    @shim('fwrite', ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_size_t, ctypes.c_void_p)
    def _fwrite(p, size, n, f):
        return libc.fwrite(ctypes.c_void_p(p), size, n, ctypes.c_void_p(_real_std.get(f, f)))


    @shim('fputs', ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
    def _fputs(s, f):
        return libc.fputs(ctypes.c_void_p(s), ctypes.c_void_p(_real_std.get(f, f)))


    @shim('fputc', ctypes.c_int, ctypes.c_int, ctypes.c_void_p)
    def _fputc(c, f):
        return libc.fputc(c, ctypes.c_void_p(_real_std.get(f, f)))


    # Emulated thread-local storage (Android x86_64 builds keep `thread_local` variables this way; glibc has no such function).
    # A control block is {size, align, index, template}; each thread gets its own copy of each variable, made on first use.
    import threading  # noqa: E402

    _TLS = threading.local()
    _TLS_INDEX: dict = {}
    libc.aligned_alloc.restype = ctypes.c_void_p
    libc.aligned_alloc.argtypes = [ctypes.c_size_t, ctypes.c_size_t]


    @shim('__emutls_get_address', ctypes.c_void_p, ctypes.c_void_p)
    def _emutls(control):
        table = getattr(_TLS, 'table', None)
        if table is None:
            table = _TLS.table = {}
        addr = table.get(control)
        if addr is None:
            size, align, _index, template = struct.unpack('<QQQQ', ctypes.string_at(control, 32))
            align = max(align, 16)
            addr = libc.aligned_alloc(align, (size + align - 1) // align * align)
            if template:
                ctypes.memmove(addr, template, size)
            else:
                ctypes.memset(addr, 0, size)
            table[control] = addr
        return addr


# ----------------------------------------------------------------------------------------------------------------------------
class Module:
    def __init__(self, path: str, elf: Elf, base: int):
        self.path, self.elf, self.base = path, elf, base
        self.exports = {s['name']: base + s['value'] for s in elf.symbols if s['defined'] and s['name']}

    def addr(self, rva: int) -> int:
        return self.base + rva

    def call(self, rva: int, *args, restype=ctypes.c_void_p):
        """Call an engine function: plain ints are passed as pointer-sized values, ctypes values as their own type."""
        if WINDOWS:
            return PW.call(self.base + rva, args, restype)
        args = [ctypes.c_void_p(a) if isinstance(a, int) else a for a in args]
        fn = ctypes.CFUNCTYPE(restype, *[type(a) for a in args])(self.base + rva)
        return fn(*args)


class Loader:
    def __init__(self):
        self.modules: list = []
        self.missing: dict = {}
        LOADERS.append(self)

    def resolve(self, name: str) -> 'int | None':
        for m in self.modules:                               # the libraries we loaded first (the engine finds its C++ runtime)
            if name in m.exports:
                return m.exports[name]
        if name in SHIMS and SHIMS[name]:
            return SHIMS[name]
        return native(name)                                  # then the node's own C library

    def load(self, path: str) -> Module:
        elf = Elf(path)
        loads = [s for s in elf.segments if s[0] == PT_LOAD]
        lo = min(s[3] for s in loads) & ~0xfff
        hi = max(s[3] + s[5] for s in loads)
        size = (hi - lo + 0xfff) & ~0xfff
        base = alloc_exec(size)
        if base is None:
            raise OSError(ctypes.get_errno(), f'allocating {size} bytes for {Path(path).name} failed')
        base -= lo
        for _t, _f, off, vaddr, filesz, _memsz, _a in loads:   # copy each segment; the rest of memsz stays zero (bss)
            ctypes.memmove(base + vaddr, elf.data[off:off + filesz], filesz)
        module = Module(path, elf, base)
        self.modules.append(module)
        self._relocate(module)
        if WINDOWS:
            PW.register_module(base, elf, Path(path).name)
        if MAC:
            PM.register_module(base, elf, Path(path).name)
        return module

    def _relocate(self, m: Module) -> None:
        elf, base = m.elf, m.base
        name_of = [s['name'] for s in elf.symbols]
        cache: dict = {}
        absolute, got, plt, relative = RELOCATIONS[elf.machine]
        for off, rtype, sym, addend in elf.relocations():
            where = base + off
            if rtype == relative:
                value = base + addend
            elif rtype in (got, plt, absolute):
                if sym not in cache:
                    s = elf.symbols[sym]
                    target = (base + s['value']) if s['defined'] and s['name'] not in SHIMS else self.resolve(s['name'])
                    if target is None:
                        self.missing.setdefault(s['name'], []).append(Path(m.path).name)
                        target = self._missing_function(s['name'])
                    if s['name'] in COUNT_IMPORTS and not s['defined']:
                        target = counted_import(s['name'], target)
                    cache[sym] = target
                value = cache[sym] + (addend if rtype == absolute else 0)
            else:
                raise NotImplementedError(f'relocation type {rtype} in {m.path}')
            struct.pack_into('<Q', (ctypes.c_char * 8).from_address(where), 0, value & 0xffffffffffffffff)

    def _missing_function(self, name: str) -> int:
        """Something no library provides: a function that reports itself loudly the moment it is called, so a missing piece is
        never silently ignored."""
        def fn(*_args):
            sys.stderr.write(f'crx: the engine called {name}, which nothing provides\n')
            STUBS_CALLED[name] = STUBS_CALLED.get(name, 0) + 1
            return 0
        return callback(ctypes.c_long)(fn)

    def run_constructors(self, m: Module) -> int:
        """The library's own start-up code (C++ global constructors), as the Android linker would run it."""
        count = 0
        if DT['INIT'] in m.elf.dyn:
            cfunc(m.base + m.elf.dyn[DT['INIT']], None)()
            count += 1
        if DT['INIT_ARRAY'] in m.elf.dyn:
            arr, n = m.base + m.elf.dyn[DT['INIT_ARRAY']], m.elf.dyn[DT['INIT_ARRAYSZ']] // 8
            for i in range(n):
                fn = struct.unpack('<Q', ctypes.string_at(arr + i * 8, 8))[0]
                if fn not in (0, 0xffffffffffffffff):
                    if os.environ.get('CRX_CTOR_TRACE'):
                        sys.stderr.write(f'crx: constructor {i} at {Path(m.path).name}+0x{fn - m.base:x}\n')
                        sys.stderr.flush()
                    cfunc(fn, None)()
                    count += 1
        if WINDOWS or MAC:                                   # code decrypted by now: make its stack-canary reads safe here
            patched = (PW if WINDOWS else PM).patch_fs_canary(m)
            if patched and os.environ.get('CRX_CTOR_TRACE'):
                sys.stderr.write(f'crx: {patched} stack-canary reads rewritten in {Path(m.path).name}\n')
        return count


# CRX_COUNT_IMPORTS=name,name,...: how often the engine calls these C library functions (a machine-code counter in front of
# each, x86_64; counters on their own page). Diagnosis only; IMPORT_COUNTS[name] = the counter's address.
COUNT_IMPORTS = set(filter(None, os.environ.get('CRX_COUNT_IMPORTS', '').split(',')))
IMPORT_COUNTS: dict = {}
_COUNTERS = {'data': None, 'code': None, 'used': 0}


def counted_import(name: str, real: int) -> int:
    if name in IMPORT_COUNTS:
        return IMPORT_COUNTS[name][1]
    if _COUNTERS['data'] is None:
        buf = ctypes.create_string_buffer(8192)
        KEEP.append(buf)
        _COUNTERS.update(data=(ctypes.addressof(buf) + 4095) & ~4095, code=alloc_exec(8192))
    k = _COUNTERS['used']
    _COUNTERS['used'] += 1
    counter, stub = _COUNTERS['data'] + 8 * k, _COUNTERS['code'] + 32 * k
    code = (b'\x49\xbb' + struct.pack('<Q', counter) + bytes.fromhex('f049ff03')          # mov r11, counter; lock inc [r11]
            + b'\xff\x25\x00\x00\x00\x00' + struct.pack('<Q', real))                     # jmp real (r11 is scratch)
    if name == os.environ.get('CRX_CALLER_OF'):          # also keep the caller: return address into a 256-slot ring
        ring = _COUNTERS['data'] + 2048
        stub = _COUNTERS['code'] + 4096
        code = (bytes.fromhex('50')                                                       # push rax
                + b'\x49\xbb' + struct.pack('<Q', counter) + bytes.fromhex('498b03')      # mov r11, counter; mov rax, [r11]
                + bytes.fromhex('48ffc0' '498903')                                        # inc rax; mov [r11], rax
                + bytes.fromhex('25ff000000')                                             # and eax, 255
                + b'\x49\xbb' + struct.pack('<Q', ring) + bytes.fromhex('4d8d1cc3')      # mov r11, ring; lea r11, [r11+rax*8]
                + bytes.fromhex('488b442408' '498903')                                    # mov rax, [rsp+8]; mov [r11], rax
                + bytes.fromhex('58')                                                     # pop rax
                + b'\xff\x25\x00\x00\x00\x00' + struct.pack('<Q', real))
        IMPORT_COUNTS['__ring__'] = (ring, 0)
    ctypes.memmove(stub, code, len(code))
    IMPORT_COUNTS[name] = (counter, stub)
    return stub


def seal_code(module, writable: bool = False) -> int:
    """Make a library's executable segments read + execute only (writable=True: writable again, to patch). A code page that
    stays writable makes Rosetta (macOS) re-check its translations constantly; on Linux this only adds safety. Returns the
    number of segments changed."""
    if WINDOWS:
        return 0
    libc.mprotect.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
    count = 0
    for t, flags, _off, vaddr, _filesz, memsz, _a in module.elf.segments:
        if t == PT_LOAD and flags & 1:
            lo = (module.base + vaddr) & ~0xfff
            hi = (module.base + vaddr + memsz + 0xfff) & ~0xfff
            prot = PROT_READ | PROT_EXEC | (PROT_WRITE if writable else 0)
            if libc.mprotect(ctypes.c_void_p(lo), hi - lo, prot) == 0:
                count += 1
    return count


# ----------------------------------------------------------------------------------------------------------------------------
# Crash reports: a native crash prints where it happened (library + offset, and the address it touched) instead of vanishing.
LOADERS: list = []


class _SigAction(ctypes.Structure):
    _fields_ = [('handler', ctypes.c_void_p), ('mask', ctypes.c_ulong * 16), ('flags', ctypes.c_int), ('restorer', ctypes.c_void_p)]


class _DlInfo(ctypes.Structure):
    _fields_ = [('fname', ctypes.c_char_p), ('fbase', ctypes.c_void_p), ('sname', ctypes.c_char_p), ('saddr', ctypes.c_void_p)]


if not WINDOWS:
    libdl.dladdr.argtypes = [ctypes.c_void_p, ctypes.POINTER(_DlInfo)]
    libdl.dladdr.restype = ctypes.c_int


def where(addr: int) -> str:
    if ARM64:
        addr &= 0x0000ffffffffffff                           # return addresses carry pointer-authentication bits up top
    for ld in LOADERS:
        for m in ld.modules:
            lo = m.base + min(s[3] for s in m.elf.segments if s[0] == PT_LOAD)
            hi = m.base + max(s[3] + s[5] for s in m.elf.segments if s[0] == PT_LOAD)
            if lo <= addr < hi:
                return f'{Path(m.path).name}+0x{addr - m.base:x}'
    if WINDOWS:
        return f'0x{addr:x}'
    info = _DlInfo()
    if addr and libdl.dladdr(addr, ctypes.byref(info)) and info.fname:      # the node's own libraries: name the function
        name = Path(info.fname.decode(errors='replace')).name
        if info.sname:
            return f'{name}:{info.sname.decode(errors="replace")}+0x{addr - (info.saddr or 0):x}'
        return f'{name}+0x{addr - (info.fbase or 0):x}'
    return f'0x{addr:x}'


def install_crash_report() -> None:
    if WINDOWS:
        PW.install_crash_report(where)
        return
    if MAC:
        PM.install_crash_report(where)
        return
    names = {11: 'SIGSEGV', 7: 'SIGBUS', 4: 'SIGILL', 6: 'SIGABRT', 8: 'SIGFPE'}

    def handler(sig, info, uctx):
        fault = ctypes.c_void_p.from_address(info + 16).value or 0
        if ARM64:                                            # mcontext at +176: fault_address, x0..x30, sp, pc
            regs = uctx + 184
            pc = ctypes.c_ulong.from_address(regs + 32 * 8).value
            sp = ctypes.c_ulong.from_address(regs + 31 * 8).value
            lines = [f'\ncrx: native crash {names.get(sig, sig)} at {where(pc)}, touching 0x{fault:x}',
                     '    lr: ' + where(ctypes.c_ulong.from_address(regs + 30 * 8).value)
                     + '   x16 (the function called through the PLT): ' + where(ctypes.c_ulong.from_address(regs + 16 * 8).value)]
            try:
                frames = [where(ctypes.c_ulong.from_address(sp + 8 * i).value) for i in range(512)]
                lines.append('    stack: ' + ' <- '.join([f for f in frames if '+' in f][:12]))
            except Exception:  # noqa: BLE001
                pass
            lines.append('    regs: ' + ' '.join(f'x{i}={ctypes.c_ulong.from_address(regs + 8 * i).value:x}' for i in range(31)))
            os.write(2, ('\n'.join(lines) + '\n').encode())
            os._exit(70)
        gregs = uctx + 40                                    # ucontext_t.uc_mcontext.gregs (x86_64 glibc)
        rip = ctypes.c_ulong.from_address(gregs + 16 * 8).value
        rsp = ctypes.c_ulong.from_address(gregs + 15 * 8).value
        lines = [f'\ncrx: native crash {names.get(sig, sig)} at {where(rip)}, touching 0x{fault:x}']
        try:                                                 # return addresses on the stack that point into our libraries
            frames = [where(ctypes.c_ulong.from_address(rsp + 8 * i).value) for i in range(256)]
            lines.append('    stack: ' + ' <- '.join([f for f in frames if '+' in f][:12]))
        except Exception:  # noqa: BLE001
            pass
        names_regs = ['r8', 'r9', 'r10', 'r11', 'r12', 'r13', 'r14', 'r15', 'rdi', 'rsi', 'rbp', 'rbx', 'rdx', 'rax', 'rcx', 'rsp', 'rip']
        regs = {n: ctypes.c_ulong.from_address(gregs + 8 * i).value for i, n in enumerate(names_regs)}
        lines.append('    regs: ' + ' '.join(f'{n}={v:x}' for n, v in regs.items()))
        try:                                                 # the code as it runs (parts of the engine are decrypted at start-up)
            lines.append(f'    code@{rip - 64:x}: ' + ctypes.string_at(rip - 64, 96).hex())
        except Exception:  # noqa: BLE001
            pass
        os.write(2, ('\n'.join(lines) + '\n').encode())
        os._exit(70)
    cb = ctypes.CFUNCTYPE(None, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)(handler)
    KEEP.append(cb)
    act = _SigAction()
    act.handler = ctypes.cast(cb, ctypes.c_void_p).value
    act.flags = 0x4                                          # SA_SIGINFO
    for sig in names:
        libc.sigaction(sig, ctypes.byref(act), None)


# ----------------------------------------------------------------------------------------------------------------------------
# Android's asset manager, backed by plain files: ASSET_ROOT is a folder with the APK's assets/ unpacked.
ASSET_ROOT = Path(os.environ.get('CRX_ASSETS', 'assets'))
ASSET_LOG: list = []
_ASSETS: dict = {}
_ASSET_MANAGER = ctypes.create_string_buffer(64)
KEEP.append(_ASSET_MANAGER)


@shim('AAssetManager_fromJava', ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
def _am_from_java(env, obj):
    return ctypes.addressof(_ASSET_MANAGER)


@shim('AAssetManager_open', ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int)
def _am_open(mgr, name, mode):
    rel = name.decode(errors='replace')
    from neo_replay import resourcefs
    path = resourcefs.resolve_asset(ASSET_ROOT, rel)
    if resourcefs.audio(rel):
        resourcefs.record(rel, False, 'audio-disabled')
        return None
    resourcefs.record(str(path.relative_to(ASSET_ROOT)) if path.is_relative_to(ASSET_ROOT) else rel, path.is_file())
    ASSET_LOG.append(rel + ('' if path.is_file() else '   (missing)'))
    if not path.is_file() and os.environ.get('CRX_TRACE_MISSING'):
        sys.stderr.write(f'crx: asset missing: {rel}\n')
    if os.environ.get('CRX_TRACE_ASSETS'):
        sys.stderr.write(f'crx: asset open: {rel}{"" if path.is_file() else "   (missing)"}\n')
    if not path.is_file():
        return None
    data = path.read_bytes()
    buf = ctypes.create_string_buffer(data, len(data) or 1)
    handle = ctypes.create_string_buffer(16)
    _ASSETS[ctypes.addressof(handle)] = {'data': buf, 'size': len(data), 'pos': 0, 'handle': handle, 'name': rel}
    return ctypes.addressof(handle)


@shim('AAsset_close', None, ctypes.c_void_p)
def _asset_close(a):
    _ASSETS.pop(a, None)


@shim('AAsset_getLength64', ctypes.c_long, ctypes.c_void_p)
def _asset_len(a):
    return _ASSETS[a]['size'] if a in _ASSETS else 0


@shim('AAsset_getRemainingLength64', ctypes.c_long, ctypes.c_void_p)
def _asset_remaining(a):
    x = _ASSETS.get(a)
    return x['size'] - x['pos'] if x else 0


@shim('AAsset_read', ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t)
def _asset_read(a, buf, count):
    x = _ASSETS.get(a)
    if not x:
        return -1
    n = max(0, min(count, x['size'] - x['pos']))
    ctypes.memmove(buf, ctypes.addressof(x['data']) + x['pos'], n)
    x['pos'] += n
    return n


@shim('AAsset_seek64', ctypes.c_long, ctypes.c_void_p, ctypes.c_long, ctypes.c_int)
def _asset_seek(a, offset, whence):
    x = _ASSETS.get(a)
    if not x:
        return -1
    x['pos'] = max(0, min(x['size'], {0: 0, 1: x['pos'], 2: x['size']}[whence] + offset))
    return x['pos']


@shim('AAsset_openFileDescriptor64', ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
def _asset_fd(a, start, length):
    return -1                                                # the engine falls back to reading


@shim('AAsset_getBuffer', ctypes.c_void_p, ctypes.c_void_p)
def _asset_buffer(a):
    x = _ASSETS.get(a)
    return ctypes.addressof(x['data']) if x else None
