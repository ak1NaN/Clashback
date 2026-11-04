"""Watch an engine function without changing what it does: its first instructions are replaced by a jump to a small stub that
hands the arguments to Python, then runs the copied instructions and jumps back. The bytes at the site are checked first (a
different build is refused), exactly like the guards.

Only for functions whose first bytes are position-independent (plain pushes / moves / compares, no rip-relative or jumps); the
caller passes those bytes, so nothing is guessed.
"""
import ctypes
import struct

import loader as L

KEEP: list = []                                     # callbacks and stubs live as long as the process


def _abs_jmp(target: int) -> bytes:
    return b'\xff\x25\x00\x00\x00\x00' + struct.pack('<Q', target)        # jmp [rip+0] ; .quad target (14 bytes)


def watch(g, rva: int, expected_hex: str, fn) -> None:
    """Call fn(rdi, rsi, rdx, return_address) every time the engine enters the function at rva. expected_hex: its first bytes (>= 14)."""
    expected = bytes.fromhex(expected_hex)
    if len(expected) < 14:
        raise ValueError('need at least the first 14 bytes (the jump overwrites them)')
    site = g.addr(rva)
    if ctypes.string_at(site, len(expected)) != expected:
        raise RuntimeError(f'hook refused: bytes at 0x{rva:x} differ from the expected ones')
    cb = L.callback(None, ctypes.c_uint64, ctypes.c_uint64, ctypes.c_uint64, ctypes.c_uint64)(
        lambda a, b, c, d: fn(a, b, c, d))
    KEEP.append(cb)
    cb_addr = ctypes.cast(cb, ctypes.c_void_p).value
    stub = bytes.fromhex(
        '57' '56' '52' '51' '4150' '4151'                  # push rdi, rsi, rdx, rcx, r8, r9 (the arguments)
        '4883ec08'                                         # sub rsp, 8: 16-byte stack alignment for the call
        '488b4c2438')                                      # rcx = the return address (the callback's 4th argument only)
    stub += b'\x48\xb8' + struct.pack('<Q', cb_addr)       # mov rax, callback
    stub += bytes.fromhex('ffd0' '4883c408' '4159' '4158' '59' '5a' '5e' '5f')   # call; restore everything
    stub += expected + _abs_jmp(site + len(expected))     # the replaced instructions, then back into the function
    mem = L.alloc_exec(4096)
    ctypes.memmove(mem, stub, len(stub))
    KEEP.append(mem)
    patch = _abs_jmp(mem) + b'\x90' * (len(expected) - 14)
    ctypes.memmove(site, patch, len(patch))


def _near(g, size=4096) -> int:
    """Executable memory within reach of a rel32 call from the engine (a call site can only be redirected that far)."""
    for delta in range(1, 64):
        hint = (g.base - delta * 0x100000) & ~0xfff
        mem = L.alloc_exec(size, hint)
        if mem and abs(mem - g.base) < 0x7f000000:
            KEEP.append(mem)
            return mem
    raise OSError('no memory near the engine for a call-site stub')


def watch_call(g, call_rva: int, fn) -> None:
    """Call fn(rdi, rsi, rdx, return_address) whenever the engine reaches the `call rel32` at call_rva; the original target then
    runs as before. For functions whose own first bytes cannot be moved (they start with a jump)."""
    site = g.addr(call_rva)
    head = ctypes.string_at(site, 5)
    if head[0] != 0xE8:
        raise RuntimeError(f'hook refused: 0x{call_rva:x} is not a call')
    target = site + 5 + struct.unpack('<i', head[1:])[0]
    cb = L.callback(None, ctypes.c_uint64, ctypes.c_uint64, ctypes.c_uint64, ctypes.c_uint64)(
        lambda a, b, c, d: fn(a, b, c, d))
    KEEP.append(cb)
    stub = bytes.fromhex('57' '56' '52' '51' '4150' '4151' '4883ec08' '488b4c2438')
    stub += b'\x48\xb8' + struct.pack('<Q', ctypes.cast(cb, ctypes.c_void_p).value)
    stub += bytes.fromhex('ffd0' '4883c408' '4159' '4158' '59' '5a' '5e' '5f')
    stub += _abs_jmp(target)
    mem = _near(g)
    ctypes.memmove(mem, stub, len(stub))
    ctypes.memmove(site, b'\xe8' + struct.pack('<i', mem - (site + 5)), 5)


# Debugger::error(const char *message, bool ...): every engine error goes through here (it then tells Sentry).
def _movable_head(g, name, need=14) -> str:
    """The first whole instructions of a named function covering >= need bytes, refused if any depends on its address."""
    import bindings
    import patch
    head = b''
    for insn in bindings.of(g).insns(name):
        if patch._relative(insn) or patch._rip_relative(insn):
            raise ValueError('{}: instruction {} cannot be moved'.format(name, insn.hex()))
        head += insn
        if len(head) >= need:
            return head.hex()
    raise ValueError('{}: not enough recorded instructions'.format(name))


def print_engine_errors(g, out=None) -> list:
    import sys
    seen: list = []

    def on_error(message, _flag, _rdx, caller):
        text = ctypes.string_at(message).decode(errors='replace') if message else '(null)'
        seen.append(text)
        (out or sys.stderr).write(f'[engine error] {text!r} from {L.where(caller)}\n')
    import bindings
    watch(g, bindings.of(g).fn('debugger_error'), _movable_head(g, 'debugger_error'), on_error)
    return seen
