"""The Mac side of crx (an x86_64 process under Rosetta; crx/mac/README.md). The engine is Android x86_64 code; a Mac x86_64
process calls with the same convention (System V), so Python calls the engine and the engine calls Python through plain ctypes.
What differs: the parts of the C library in crx/mac/bionic_mac.c (functions exported as a_<name>, looked up first), the stack
canary (fs:[0x28], rewritten after start-up as on Windows), the module list C++ exceptions walk, and crash reports.
"""
import ctypes
import os
import struct
from pathlib import Path

DYLIB = Path(os.environ.get('CRX_BIONIC_MAC', Path.home() / '.cache/crx-render/mac/bionic_mac.dylib'))
dll = ctypes.CDLL(str(DYLIB), mode=ctypes.RTLD_GLOBAL)
libc = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
RTLD_DEFAULT = ctypes.c_void_p(-2)
libc.dlsym.restype = ctypes.c_void_p
libc.dlsym.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
libc.mmap.restype = ctypes.c_void_p
libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_long]
libc.munmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
dll.crx_register_module.argtypes = [ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p]
MAP_PRIVATE, MAP_ANON = 0x02, 0x1000
KEEP: list = []


def native(name: str) -> 'int | None':
    """bionic_mac's version of an Android C library function (a_<name>) when it has one, else macOS's own."""
    return libc.dlsym(dll._handle, ('a_' + name).encode()) or libc.dlsym(RTLD_DEFAULT, name.encode()) or None


def alloc_exec(size: int, hint: 'int | None' = None) -> 'int | None':
    mem = libc.mmap(hint, size, 7, MAP_PRIVATE | MAP_ANON, -1, 0)
    return None if mem in (None, ctypes.c_void_p(-1).value) else mem


def free_exec(addr: int, size: int) -> None:
    libc.munmap(ctypes.c_void_p(addr), size)


def register_module(base: int, elf, name: str) -> None:
    """Tell bionic_mac's dl_iterate_phdr where a library's program headers are (C++ exception unwinding looks them up)."""
    phoff = struct.unpack_from('<Q', elf.data, 0x20)[0]
    phnum = struct.unpack_from('<H', elf.data, 0x38)[0]
    dll.crx_register_module(base, ctypes.c_void_p(base + phoff), phnum, name.encode())


# The stack canary: Android code reads it with `mov reg, fs:[0x28]`; a Mac process has no fs base set up, so each read would
# fault. After start-up (part of the engine's code is decrypted then) each one is rewritten to read one fixed value instead,
# exactly as crx/plat_win.py does:  64 REX 8B ModRM 25 28 00 00 00  ->  REX 8B ModRM(rip-relative) disp32 66 90.
_CANARY: dict = {}


def _near_alloc(base: int, size: int) -> int:
    for delta in range(1, 512):
        for hint in ((base - delta * 0x100000) & ~0xffff, (base + 0x4000000 + delta * 0x100000) & ~0xffff):
            mem = alloc_exec(size, hint)
            if mem and abs(mem - base) < 0x70000000:
                return mem
            if mem:
                free_exec(mem, size)
    raise OSError('crx: no memory near the engine for the stack canary')


def patch_fs_canary(module) -> int:
    seg_lo = min(s[3] for s in module.elf.segments if s[0] == 1)
    if module.base not in _CANARY:
        mem = _near_alloc(module.base + seg_lo, 0x1000)
        ctypes.c_uint64.from_address(mem).value = 0x2f8dd23ac5e9a147          # any constant: only equality is checked
        _CANARY[module.base] = mem
    canary = _CANARY[module.base]
    count = 0
    for t, flags, _off, vaddr, _filesz, memsz, _a in module.elf.segments:
        if t != 1 or not flags & 1:
            continue
        start = module.base + vaddr
        code = bytearray(ctypes.string_at(start, memsz))
        i = code.find(b'\x64')
        while i >= 0 and i + 9 <= len(code):
            rex, op, modrm = code[i + 1], code[i + 2], code[i + 3]
            if 0x48 <= rex <= 0x4f and op == 0x8B and (modrm & 0xC7) == 0x04 and code[i + 4:i + 9] == b'\x25\x28\x00\x00\x00':
                at = start + i
                new = bytes([rex & 0xFC, 0x8B, (modrm & 0x38) | 0x05]) + struct.pack('<i', canary - (at + 7)) + b'\x66\x90'
                ctypes.memmove(at, new, 9)
                code[i:i + 9] = new
                count += 1
                i = code.find(b'\x64', i + 9)
            else:
                i = code.find(b'\x64', i + 1)
    return count


class _SigAction(ctypes.Structure):           # macOS: handler, mask (u32), flags
    _fields_ = [('handler', ctypes.c_void_p), ('mask', ctypes.c_uint32), ('flags', ctypes.c_int)]


def install_crash_report(where) -> None:
    names = {11: 'SIGSEGV', 10: 'SIGBUS', 4: 'SIGILL', 6: 'SIGABRT', 8: 'SIGFPE'}   # SIGSYS: bionic_mac emulates raw Linux syscalls

    def handler(sig, info, uctx):
        fault = ctypes.c_void_p.from_address(info + 24).value or 0            # siginfo.si_addr
        mc = ctypes.c_void_p.from_address(uctx + 48).value                     # ucontext.uc_mcontext
        ss = mc + 16                                                           # after the exception state
        regs = ['rax', 'rbx', 'rcx', 'rdx', 'rdi', 'rsi', 'rbp', 'rsp'] + [f'r{i}' for i in range(8, 16)] + ['rip']
        v = {n: ctypes.c_uint64.from_address(ss + 8 * i).value for i, n in enumerate(regs)}
        lines = [f'\ncrx: native crash {names.get(sig, sig)} at {where(v["rip"])}, touching 0x{fault:x}']
        try:
            frames = [where(ctypes.c_uint64.from_address(v['rsp'] + 8 * i).value) for i in range(256)]
            lines.append('    stack: ' + ' <- '.join([f for f in frames if '+' in f][:12]))
        except Exception:  # noqa: BLE001
            pass
        lines.append('    regs: ' + ' '.join(f'{n}={x:x}' for n, x in v.items()))
        os.write(2, ('\n'.join(lines) + '\n').encode())
        os._exit(70)
    cb = ctypes.CFUNCTYPE(None, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)(handler)
    KEEP.append(cb)
    act = _SigAction(ctypes.cast(cb, ctypes.c_void_p).value, 0, 0x40)        # SA_SIGINFO
    for sig in names:
        libc.sigaction(sig, ctypes.byref(act), None)
