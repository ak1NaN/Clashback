"""Engine patches described as data and assembled for whichever build is loaded (addresses from bindings.py).

A patch replaces the first `replace` instructions at a named place (a function or a code site). Its `ops` are assembled either in
place (`stub=False`: must fit in the replaced bytes; the rest becomes NOPs) or in a stub next to the engine that the place jumps
to (`stub=True`). Before anything is written, the replaced instructions must equal the bytes recorded for that build; a different
build is refused, never patched blindly. Every write can be undone (the temporary patches around GameMain::init and each update).

ops:
  ('raw', 'hex')        bytes as they are (only position-independent code)
  ('copy', k)           the place's own instruction k (refused if it is relative to its address)
  ('jmp'|'je'|'jne'|'call', target)
  ('back', k)           jump to the place's instruction k (the code after the replaced ones)
  ('push_back', k)      push the address of instruction k (a call that must return there: the dispatcher reads the return address)
  ('label', '.name')
targets: a binding name (function / thunk / code site), '.name' (a label in the same patch), '@k' (where the place's instruction k
jumps or calls to).
"""
import ctypes
import struct

import bindings
import loader as L

_STUB_SIZE = 4096
_POOL = []


def _near(g, size=128) -> int:
    """Executable memory within reach of a rel32 jump from the engine, carved from one pool mapped next to the library
    (searched below and above it)."""
    pool = _POOL[-1] if _POOL else None
    if pool is None or pool[1] + size > pool[2]:
        base = None
        lo = g.base
        hi = g.base + max(s[3] + s[5] for s in g.elf.segments if s[0] == 1)
        for delta in range(1, 512):
            for hint in ((lo - delta * 0x100000) & ~0xfff, (hi + delta * 0x100000) & ~0xfff):
                mem = L.alloc_exec(_STUB_SIZE * 16, hint)
                if mem and abs(mem - g.base) < 0x7f000000 and abs(mem - hi) < 0x7f000000:
                    base = mem
                    break
                if mem:
                    L.free_exec(mem, _STUB_SIZE * 16)
            if base:
                break
        if base is None:
            raise OSError('no memory near the engine for a patch stub')
        pool = [base, base, base + _STUB_SIZE * 16]
        _POOL.append(pool)
    where = pool[1]
    pool[1] += (size + 15) & ~15
    return where


def _relative(insn: bytes):
    """(kind, target offset field, length of field) for a relative branch/call, else None."""
    i = 1 if insn[0] in (0x40, 0x41, 0x48, 0x49) and len(insn) > 1 and insn[1] in (0xE8, 0xE9) else 0
    op = insn[i]
    if op in (0xE8, 0xE9):
        return i + 1, 4
    if op == 0xEB or 0x70 <= op <= 0x7F:
        return i + 1, 1
    if op == 0x0F and len(insn) > i + 1 and 0x80 <= insn[i + 1] <= 0x8F:
        return i + 2, 4
    return None


def _rip_relative(insn: bytes) -> bool:
    """Conservative: any ModRM with mod=00 rm=101 after a plain opcode means [rip+disp32]."""
    i = 0
    while i < len(insn) and insn[i] in (0x66, 0x67, 0xF2, 0xF3, 0x2E, 0x3E, 0x26, 0x64, 0x65, 0x36):
        i += 1
    if i < len(insn) and 0x40 <= insn[i] <= 0x4F:
        i += 1
    if i >= len(insn):
        return False
    if insn[i] == 0x0F:
        i += 2 if i + 1 < len(insn) and insn[i + 1] in (0x38, 0x3A) else 1
    i += 1
    return i < len(insn) and (insn[i] & 0xC7) == 0x05


def _target_of(insn: bytes, at: int) -> int:
    rel = _relative(insn)
    if rel is None:
        raise ValueError('instruction {} is not a relative branch or call'.format(insn.hex()))
    pos, size = rel
    disp = struct.unpack('<i' if size == 4 else '<b', insn[pos:pos + size])[0]
    return at + len(insn) + disp


class Patch:
    def __init__(self, place: str, replace: int, ops: list, stub: bool):
        self.place, self.replace, self.ops, self.stub = place, replace, ops, stub

    def _assemble(self, g, B, here: int, insns: list, site: int) -> bytes:
        starts = [site]
        for insn in insns:
            starts.append(starts[-1] + len(insn))
        labels, out = {}, b''
        for rnd in (0, 1):
            out, pos = b'', here
            for op in self.ops:
                kind, arg = op[0], op[1]
                if kind == 'label':
                    labels[arg] = pos
                    continue
                if kind == 'raw':
                    chunk = bytes.fromhex(arg)
                elif kind == 'copy':
                    insn = insns[arg]
                    if _relative(insn) or _rip_relative(insn):
                        raise ValueError('{}: instruction {} ({}) depends on its address; cannot be copied'
                                         .format(self.place, arg, insn.hex()))
                    chunk = insn
                elif kind == 'push_back':
                    chunk = b'\x49\xbb' + struct.pack('<Q', starts[arg]) + b'\x41\x53'      # mov r11, imm64; push r11
                else:
                    if kind == 'back':
                        target = starts[arg]
                    elif isinstance(arg, str) and arg.startswith('.'):
                        target = labels.get(arg, pos) if rnd else pos
                    elif isinstance(arg, str) and arg.startswith('@'):
                        k = int(arg[1:])
                        target = _target_of(insns[k], starts[k])
                    else:
                        target = g.addr(B.addr(arg))
                    opcode = {'jmp': b'\xe9', 'back': b'\xe9', 'call': b'\xe8', 'je': b'\x0f\x84', 'jne': b'\x0f\x85'}[kind]
                    end = pos + len(opcode) + 4
                    disp = target - end
                    if not -2 ** 31 <= disp < 2 ** 31:
                        raise ValueError('{}: target out of rel32 reach'.format(self.place))
                    chunk = opcode + struct.pack('<i', disp)
                out += chunk
                pos += len(chunk)
        return out

    def apply(self, g, B=None):
        """Check, then write. Returns the undo record."""
        B = B or bindings.of(g)
        insns = B.insns(self.place)[:self.replace]
        if len(insns) < self.replace:
            raise ValueError('{}: only {} instructions recorded'.format(self.place, len(insns)))
        site = g.addr(B.addr(self.place))
        original = b''.join(insns)
        live = ctypes.string_at(site, len(original))
        if live != original:
            raise RuntimeError('patch {} refused: bytes at 0x{:x} are {} not {} (a different build?)'
                               .format(self.place, B.addr(self.place), live.hex(), original.hex()))
        if self.stub:
            mem = _near(g, 256)
            code = self._assemble(g, B, mem, insns, site)
            ctypes.memmove(mem, code, len(code))
            jump = b'\xe9' + struct.pack('<i', mem - (site + 5))
            if len(jump) > len(original):
                raise ValueError('{}: replaced instructions are shorter than a jump'.format(self.place))
            new = jump + b'\x90' * (len(original) - len(jump))
        else:
            code = self._assemble(g, B, site, insns, site)
            if len(code) > len(original):
                raise ValueError('{}: patch ({} bytes) does not fit in {} bytes'.format(self.place, len(code), len(original)))
            new = code + b'\x90' * (len(original) - len(code))
        ctypes.memmove(site, new, len(new))
        return (site, original)


def undo(record) -> None:
    site, original = record
    ctypes.memmove(site, original, len(original))


def ret(place: str, replace: int = 1) -> Patch:
    return Patch(place, replace, [('raw', 'c3')], stub=False)


def ret0(place: str, replace: int) -> Patch:
    return Patch(place, replace, [('raw', '31c0c3')], stub=False)          # xor eax, eax; ret


def nop(place: str, replace: int = 1) -> Patch:
    return Patch(place, replace, [], stub=False)


def jump(place: str, target: str, replace: int) -> Patch:
    return Patch(place, replace, [('jmp', target)], stub=False)
