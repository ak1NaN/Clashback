"""GATE 2e (exploration first): after gate 2d, drive the state manager the way the sandbox does (GameStateManager::update(manager,
dt) once per frame) and watch the manager's state and the data-table globals. A crash names its exact place (crash reporter), so
the sandbox's matching guard can be added for that place only.

    python3 tools/crx/gate2e.py LIBDIR WORKDIR ASSETS [frames]
"""
import ctypes
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import boot  # noqa: E402
import gate2c as C  # noqa: E402
import bindings  # noqa: E402
import guards  # noqa: E402
import hooks  # noqa: E402
import loader as L  # noqa: E402
import tables as T  # noqa: E402



def F(name):
    return bindings.of().field(name)


def pump(g, manager, dt):
    """One GameStateManager::update(manager, dt) with the per-update guards (as the sandbox's direct pump)."""
    return guards.around_update(g, lambda: g.call(bindings.of(g).fn('manager_update'), manager, ctypes.c_float(dt)))


def snapshot(g):
    manager = C.g64(g, C.R['manager_global'])
    state = ctypes.c_int32.from_address(manager + F('manager.state_type')).value if manager else None
    pending = ctypes.c_int32.from_address(manager + F('manager.pending_type')).value if manager else None
    out = {'state': state, 'pending': pending}
    loading = ctypes.c_uint64.from_address(manager + F('manager.state')).value if manager and state == 1 else 0
    if loading:
        out['phase'] = ctypes.c_int32.from_address(loading + F('loading.phase')).value
        task = ctypes.c_uint64.from_address(loading + F('loading.task')).value
        if task:
            tables = ctypes.c_uint64.from_address(task + F('task.tables')).value
            resources = ctypes.c_uint64.from_address(task + F('task.resources')).value
            out.update(task_state=ctypes.c_int32.from_address(task + F('task.state')).value,
                       task_progress=ctypes.c_int32.from_address(task + F('task.progress')).value,
                       resources=ctypes.c_int32.from_address(resources + F('resources.count')).value if resources else None,
                       tables_complete=ctypes.c_uint8.from_address(tables + F('tables.complete')).value if tables else None)
    return out


def main() -> int:
    libdir, work, assets = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    frames = int(sys.argv[4]) if len(sys.argv) > 4 else 200
    g, vm, info = boot.boot(libdir, work, assets)
    manager = C.g64(g, C.R['manager_global'])
    hooks.print_engine_errors(g)                               # the engine's own error messages, as it reports them
    print('  guards installed:', guards.install(g))
    last, opened = None, 0
    for frame in range(frames):
        state = ctypes.c_int32.from_address(manager + F('manager.state_type')).value
        delta = 0.05 if state != 4 else 0.0                     # the sandbox: 0.05 s per frame outside battle (state 4)
        pump(g, manager, delta)
        snap = snapshot(g)
        if snap != last:
            print(f'  frame {frame:4d}: {snap}  files read via asset manager so far {len(L.ASSET_LOG)}')
            last = snap
        time.sleep(0.001)
        if T.complete(T.tables_object(manager)):
            print(f'  data tables complete at frame {frame}')
            break
    print('  final:', snapshot(g))
    tables = T.tables_object(manager)
    if not T.complete(tables):
        print('GATE 2e: FAIL (the data tables did not finish)')
        return 1
    # the cards' own data files (csv_logic/characters/knight.toml, giant.toml in content 15.535.29)
    expected = {'Knight': dict(Hitpoints=690, Damage=79, HitSpeed=1200, Range=1200, Speed=60, DeployTime=1000, SightRange=5500,
                               Mass=6, CollisionRadius=500, LoadTime=700),
                'Giant': dict(Hitpoints=1550, Damage=99, HitSpeed=1500, Range=1200, Speed=45, DeployTime=1000, SightRange=7500,
                              Mass=18, CollisionRadius=750, LoadTime=1000)}
    slot = T.find_slot(tables)
    print('  characters table slot: 0x{:x} (recorded 0x{:x})'.format(slot or 0, F('tables.characters')))
    if slot is None:
        print('GATE 2e: FAIL (no characters table found)')
        return 1
    if slot != F('tables.characters'):
        print('GATE 2e: FAIL (characters table is at slot 0x{:x}: update tables.characters in the bindings)'.format(slot))
        return 1
    ok = True
    for card, want in expected.items():
        got = T.character(tables, card)
        print(f'  {card}: {got}')
        ok &= got == want
    print(f'  characters table: {len(T.rows(tables))} rows')
    print('GATE 2e:', 'PASS' if ok else 'FAIL (stats differ from the data files)')
    return 0 if ok else 1


if __name__ == '__main__':
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)      # the engine's own threads are still running; a normal interpreter shutdown under them crashes
