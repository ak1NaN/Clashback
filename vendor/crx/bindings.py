"""Engine addresses by name, per build. Each engine build has crx/bindings/<build>.json (made by port/record.py from hand-verified
addresses, or by port/port.py from an older build's file). The right file is picked by the libg.so checksum, so the same code runs
any supported build and refuses an unknown one.

    B = bindings.of(g)          # g: the loaded libg module
    B.fn('manager_update')      # function rva
    B.glob('manager_global')    # data rva
    B.code('ui_locale_call')    # rva of an instruction inside a function
    B.field('manager.state')    # struct offset
    B.insns('ui_locale_call')   # the instructions expected there (bytes), first few
"""
import ctypes
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent / 'bindings'
_CACHE = {}


class Bindings:
    def __init__(self, data: dict, g):
        self.data, self.g, self.build = data, g, data['build']

    def _entry(self, kinds, name):
        for kind in kinds:
            entry = self.data.get(kind, {}).get(name)
            if entry is not None:
                return entry
        raise KeyError('build {} has no binding "{}"'.format(self.build, name))

    def fn(self, name) -> int:
        return int(self._entry(('functions', 'thunks'), name)['rva'], 16)

    def glob(self, name) -> int:
        entry = self._entry(('globals',), name)
        if 'deref' in entry:
            raise ValueError('{} is reached through a pointer in build {}: use location()'.format(name, self.build))
        return int(entry['rva'], 16)

    def location(self, name) -> int:
        """Absolute address of a global, following a pointer when the build keeps it inside an object
        ({'rva': pointer, 'deref': offset}); 0 while that object does not exist yet."""
        entry = self._entry(('globals',), name)
        where = self.g.addr(int(entry['rva'], 16))
        if 'deref' not in entry:
            return where
        obj = ctypes.c_uint64.from_address(where).value
        return obj + int(entry['deref'], 16) if obj else 0

    def code(self, name) -> int:
        return int(self._entry(('code',), name)['rva'], 16)

    def addr(self, name) -> int:
        """rva of any named place (function, thunk, code site)."""
        return int(self._entry(('functions', 'thunks', 'code'), name)['rva'], 16)

    def insns(self, name) -> list:
        return [bytes.fromhex(h) for h in self._entry(('functions', 'thunks', 'code'), name)['insns']]

    def field(self, name) -> int:
        return int(self.data['fields'][name], 16)

    def has(self, name) -> bool:
        return any(name in self.data.get(k, {}) for k in ('functions', 'thunks', 'code', 'globals'))

    def check(self, names=None) -> list:
        """Places whose live bytes differ from the recorded instructions (an empty list = this is the build the file describes)."""
        problems = []
        for kind in ('functions', 'thunks', 'code'):
            for name, entry in self.data.get(kind, {}).items():
                if names is not None and name not in names:
                    continue
                want = bytes.fromhex(''.join(entry['insns'][:3]))
                got = ctypes.string_at(self.g.addr(int(entry['rva'], 16)), len(want))
                if got != want:
                    problems.append('{} {} at 0x{}: {} != {}'.format(kind, name, entry['rva'][2:], got.hex(), want.hex()))
        return problems


def sha256(path) -> str:
    h = hashlib.sha256()
    with open(str(path), 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def available() -> dict:
    """libg sha256 -> bindings file, for every build this checkout supports."""
    out = {}
    for path in sorted(p for p in HERE.glob('[0-9]*.json') if '.report.' not in p.name):
        data = json.loads(path.read_text(encoding='utf-8'))
        if data.get('libg_sha256'):
            out[data['libg_sha256']] = path
    return out


_LOADED = []


def use(g) -> None:
    """Remember the loaded engine (start-up calls this), so code without a handle on it can still look names up."""
    _LOADED[:] = [g]


def of(g=None) -> Bindings:
    if g is None:
        if not _LOADED:
            raise RuntimeError('no engine loaded yet')
        g = _LOADED[0]
    key = id(g)
    if key not in _CACHE:
        sha = sha256(g.path)
        files = available()
        if sha not in files:
            raise RuntimeError('no bindings for this libg.so (sha256 {}...): supported builds are {}. Port it with port/port.py'
                               .format(sha[:16], sorted(json.loads(p.read_text())['build'] for p in files.values())))
        _CACHE[key] = Bindings(json.loads(files[sha].read_text(encoding='utf-8')), g)
    return _CACHE[key]
