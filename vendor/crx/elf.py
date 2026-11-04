"""Read an Android x86_64 shared library the way the dynamic linker does: from its program headers and dynamic section only
(the engine's libg.so has no section table). Pure Python, standard library only.

    python3 tools/crx/elf.py libg.so            # summary and the functions it needs from other libraries
"""
import struct
import sys

PT_LOAD, PT_DYNAMIC, PT_TLS = 1, 2, 7
DT = {'NEEDED': 1, 'PLTRELSZ': 2, 'HASH': 4, 'STRTAB': 5, 'SYMTAB': 6, 'RELA': 7, 'RELASZ': 8, 'STRSZ': 10, 'INIT': 12,
      'JMPREL': 23, 'INIT_ARRAY': 25, 'INIT_ARRAYSZ': 27, 'GNU_HASH': 0x6ffffef5, 'RELR': 36, 'RELRSZ': 35, 'ANDROID_RELA': 0x60000011,
      'ANDROID_RELASZ': 0x60000012}


class Elf:
    def __init__(self, path: str):
        self.data = open(path, 'rb').read()
        d = self.data
        if d[:4] != b'\x7fELF' or d[4] != 2:
            raise ValueError('not a 64-bit ELF file')
        self.machine, = struct.unpack_from('<H', d, 0x12)
        phoff, = struct.unpack_from('<Q', d, 0x20)
        phentsize, phnum = struct.unpack_from('<HH', d, 0x36)
        self.segments = []                                   # (type, flags, offset, vaddr, filesz, memsz, align)
        for i in range(phnum):
            p = struct.unpack_from('<IIQQQQQQ', d, phoff + i * phentsize)
            self.segments.append((p[0], p[1], p[2], p[3], p[5], p[6], p[7]))
        self.dyn = {}
        self.needed = []
        dyn = next(s for s in self.segments if s[0] == PT_DYNAMIC)
        off = self.offset(dyn[3])
        while True:
            tag, val = struct.unpack_from('<qQ', d, off)
            off += 16
            if tag == 0:
                break
            if tag == DT['NEEDED']:
                self.needed.append(val)
            else:
                self.dyn.setdefault(tag, val)
        self.needed = [self.string(v) for v in self.needed]
        self.symbols = self._symbols()

    def offset(self, vaddr: int) -> int:
        for t, _f, off, va, filesz, _m, _a in self.segments:
            if t == PT_LOAD and va <= vaddr < va + filesz:
                return off + vaddr - va
        raise ValueError(f'address 0x{vaddr:x} is not in the file')

    def string(self, index: int) -> str:
        start = self.offset(self.dyn[DT['STRTAB']]) + index
        return self.data[start:self.data.index(b'\0', start)].decode()

    def _symbol_count(self) -> int:
        d = self.data
        if DT['HASH'] in self.dyn:
            return struct.unpack_from('<II', d, self.offset(self.dyn[DT['HASH']]))[1]
        h = self.offset(self.dyn[DT['GNU_HASH']])           # GNU hash: the last chain end gives the highest symbol index
        nbuckets, symoffset, bloom_size, _shift = struct.unpack_from('<IIII', d, h)
        buckets = struct.unpack_from(f'<{nbuckets}I', d, h + 16 + bloom_size * 8)
        last = max(buckets)
        if last < symoffset:
            return symoffset
        chains = h + 16 + bloom_size * 8 + nbuckets * 4
        while not struct.unpack_from('<I', d, chains + (last - symoffset) * 4)[0] & 1:
            last += 1
        return last + 1

    def _symbols(self):
        out = []
        base = self.offset(self.dyn[DT['SYMTAB']])
        for i in range(self._symbol_count()):
            name, info, _other, shndx, value, size = struct.unpack_from('<IBBHQQ', self.data, base + i * 24)
            out.append({'name': self.string(name), 'bind': info >> 4, 'type': info & 15, 'defined': shndx != 0, 'value': value, 'size': size})
        return out

    def relocations(self):
        """(offset, type, symbol index, addend) from DT_RELA and the PLT (DT_JMPREL). Android's packed (APS2) relocations are not handled."""
        out = []
        for start_tag, size_tag in ((DT['RELA'], DT['RELASZ']), (DT['JMPREL'], DT['PLTRELSZ'])):
            if start_tag in self.dyn:
                base, size = self.offset(self.dyn[start_tag]), self.dyn[size_tag]
                for k in range(0, size, 24):
                    r_offset, r_info, addend = struct.unpack_from('<QQq', self.data, base + k)
                    out.append((r_offset, r_info & 0xffffffff, r_info >> 32, addend))
        return out


def main():
    e = Elf(sys.argv[1])
    print(f'machine {e.machine} (62 = x86_64), {len(e.segments)} segments, needs: {", ".join(e.needed)}')
    print(f'{len(e.symbols)} dynamic symbols; packed Android relocations: {DT["ANDROID_RELA"] in e.dyn}; RELR: {DT["RELR"] in e.dyn}')
    rel = e.relocations()
    kinds = {}
    for _o, t, _s, _a in rel:
        kinds[t] = kinds.get(t, 0) + 1
    print(f'{len(rel)} relocations by type: {kinds}')
    imports = sorted({s['name'] for s in e.symbols if not s['defined'] and s['name']})
    print(f'{len(imports)} functions/objects needed from other libraries:')
    print(' '.join(imports))


if __name__ == '__main__':
    main()
