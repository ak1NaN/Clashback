"""The engine's loaded data tables (engine 15.535.29 x86_64), as measured at gate 2e.

The tables object comes from the data-load task (manager +0x20 -> loading +0x10 -> task +0 -> tables); +0x6E8 is set once every
table is built. Each table is a pointer in it; a table keeps its rows at +0x30 (pointer) with the count at +0x3C; a row's name is
an engine string at +40.
"""
import ctypes

import bindings

# Offsets (tables.complete, tables.characters, table.rows/count, row.name) come from the build's bindings. In 150535029 the
# characters table is slot +0x110 (451 rows, Knight first); other slots: buildings +0x118, spells +0xe0, projectiles +0x50.
# character row fields (int32), each matched against two cards' TOML values (Knight and Giant) -- see docs/CR_ENGINE_ON_LINUX.md
CHARACTER_FIELDS = {'Speed': 0x414, 'HitSpeed': 0x418, 'Range': 0x420, 'SightRange': 0x434, 'LoadTime': 0x48C,
                    'DeployTime': 0x490, 'CollisionRadius': 0x4E4, 'Mass': 0x4EC, 'Hitpoints': 0x7A8, 'Damage': 0x7B0}


def u64(a):
    return ctypes.c_uint64.from_address(a).value


def i32(a):
    return ctypes.c_int32.from_address(a).value


def text(a):
    n = i32(a + 4)
    src = a + 8 if n < 8 else u64(a + 8)
    return ctypes.string_at(src, n).decode(errors='replace')


def F(name):
    return bindings.of().field(name)


def tables_object(manager):
    loading = u64(manager + F('manager.state'))
    task = u64(loading + F('loading.task')) if loading else 0
    return u64(task + F('task.tables')) if task else 0


def complete(tables):
    return bool(tables) and ctypes.c_uint8.from_address(tables + F('tables.complete')).value == 1


def rows(tables, slot=None):
    t = u64(tables + (F('tables.characters') if slot is None else slot))
    base, count = u64(t + F('table.rows')), i32(t + F('table.count'))
    return {text(u64(base + 8 * i) + F('row.name')): u64(base + 8 * i) for i in range(count)}


def _readable_ranges():
    import sys
    if sys.platform == 'win32':
        import plat_win
        return plat_win.readable_ranges()
    out = []
    with open('/proc/self/maps') as f:
        for line in f:
            addr, perms = line.split()[:2]
            if perms[0] == 'r':
                lo, hi = (int(x, 16) for x in addr.split('-'))
                out.append((lo, hi))
    return out


def _ok(ranges, a, n=8):
    return a > 0 and any(lo <= a and a + n <= hi for lo, hi in ranges)


def safe_names(tables, slot, ranges, limit=6000):
    """Row names of the table in `slot`, reading only mapped memory; None if anything does not look like a table."""
    if not _ok(ranges, tables + slot):
        return None
    t = u64(tables + slot)
    if not _ok(ranges, t, 0x40):
        return None
    base, count = u64(t + F('table.rows')), i32(t + F('table.count'))
    if not 0 < count <= limit or not _ok(ranges, base, count * 8):
        return None
    names = set()
    for i in range(count):
        row = u64(base + 8 * i)
        if not _ok(ranges, row + F('row.name'), 16):
            return None
        n = i32(row + F('row.name') + 4)
        if not 0 < n <= 200:
            continue
        src = row + F('row.name') + 8 if n < 8 else u64(row + F('row.name') + 8)
        if _ok(ranges, src, n):
            names.add(ctypes.string_at(src, n).decode(errors='replace'))
    return names


def find_slot(tables, names=('Knight', 'Giant', 'HogRider'), min_rows=300, span=None):
    """The tables-object slot whose table has rows named `names` (the characters table): checks the recorded slot first, then
    every slot. Used to confirm (or find) tables.characters on a new build."""
    span = span or F('tables.complete')
    ranges = _readable_ranges()
    slots = [F('tables.characters')] + [s for s in range(0, span, 8) if s != F('tables.characters')]
    for slot in slots:
        found = safe_names(tables, slot, ranges)
        if found and len(found) >= min_rows and all(n in found for n in names):
            return slot
    return None


def character_fields():
    """The character row layout for the loaded build: from its bindings if recorded there, else the 150535029 one."""
    recorded = bindings.of().data.get('character_fields')
    return {k: int(v, 16) for k, v in recorded.items()} if recorded else CHARACTER_FIELDS


def character(tables, name):
    row = rows(tables)[name]
    return {field: i32(row + off) for field, off in character_fields().items()}


def find_fields(tables, cards: dict, span=0x1000) -> dict:
    """Calibrate the character row layout from known values: cards = {name: {field: value from its data file}}. A field's
    offset is the one place (4-byte aligned) that holds the right value for EVERY card given. Used when porting to a new build."""
    table = rows(tables)
    found = {}
    fields = set.intersection(*(set(v) for v in cards.values()))
    for field in sorted(fields):
        hits = None
        for card, values in cards.items():
            row = table[card]
            here = {o for o in range(0, span, 4) if i32(row + o) == values[field]}
            hits = here if hits is None else hits & here
        found[field] = sorted(hits)
    return found
