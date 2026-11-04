"""GATE 3: a battle builds and ticks. After gate 2e (data tables), the sandbox's direct-host recipe (jni_bridge.cpp
nativeLoadReplay + nativePumpManager + nativeStep, x86_64 150535029):

  1. the replay JSON is parsed by the engine's own parser and put at manager+0x78; pending state 4 (battle), +0x54 = -1,
     +0x10 = 3, the skip-core-and-presentation flag on;
  2. manager updates (0.05 s outside state 4, 0 inside) create BattleGameState; once the logic battle exists, its location's
     battlefield is built if missing and the OUTER replay loader runs (the inner JSON loader gives wrong RNG/start), until both
     players exist; then one outer state update;
  3. each tick: battle core update (state, 0.05) once, then the outer state update with the skip flag set (it skips the duplicate
     core call and the HUD).

    python3 tools/crx/gate3.py LIBDIR WORKDIR ASSETS [ticks]
"""
import ctypes
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import loader as L  # noqa: E402
import bindings  # noqa: E402
import boot  # noqa: E402
import gate2c as C  # noqa: E402
import gate2e as E  # noqa: E402
import guards  # noqa: E402
import hooks  # noqa: E402
import tables as T  # noqa: E402



class _Fn:
    """A.NAME -> the loaded build's function rva (bindings)."""
    NAMES = {'NATIVE_ALLOC': 'native_alloc', 'NATIVE_FREE': 'native_free', 'STRING_FROM_UTF8': 'string_from_utf8', 'STRING_DESTROY': 'string_destroy', 'PARSE_JSON_OBJECT': 'parse_json_object', 'JSON_GET_OBJECT': 'json_get_object', 'JSON_GET_DATA': 'json_get_data', 'BATTLE_STATE_UPDATE': 'battle_state_update', 'BATTLE_CORE_UPDATE': 'battle_core_update', 'BATTLE_LOAD_REPLAY': 'battle_load_replay', 'LOCATION_BATTLEFIELD': 'location_battlefield', 'LOCATION_PRIMARY': 'location_primary', 'LOCATION_SECONDARY': 'location_secondary', 'DATA_STRING_RESOLVE': 'data_string_resolve', 'RESOURCE_LOOKUP': 'resource_lookup', 'RESOURCE_LIST_CTOR': 'resource_list_ctor', 'RESOURCE_LIST_APPEND': 'resource_list_append', 'RESOURCE_LIST_LOAD': 'resource_list_load', 'RESOURCE_LIST_DTOR': 'resource_list_dtor', 'BATTLEFIELD_CACHE_ROOT': 'battlefield_cache_root', 'DATA_ROW_INSTANCE_ID': 'data_row_instance_id', 'BUILD_BATTLEFIELD_CACHE': 'build_battlefield_cache', 'PLAYER_ELIXIR': 'player_elixir'}

    def __getattr__(self, name):
        return bindings.of().fn(self.NAMES[name])


A = _Fn()


def F(name):
    return bindings.of().field(name)


KEEP = []


def u64(a):
    return ctypes.c_uint64.from_address(a).value


def i32(a):
    return ctypes.c_int32.from_address(a).value


def engine_string(g, text: str):
    buf = (ctypes.c_uint8 * 16)()
    KEEP.append(buf)
    g.call(A.STRING_FROM_UTF8, ctypes.addressof(buf), ctypes.c_char_p(text.encode()))
    return buf


def skip_flag(g, value=1):
    flag = bindings.of().location('skip_core_and_presentation_flag')
    if not flag:
        raise RuntimeError('the skip flag object does not exist yet')
    ctypes.c_uint8.from_address(flag).value = value


def load_replay(g, manager, replay_text: str) -> int:
    native = g.call(A.NATIVE_ALLOC, 16)
    g.call(A.STRING_FROM_UTF8, native, ctypes.c_char_p(replay_text.encode()))
    parsed = g.call(A.PARSE_JSON_OBJECT, native, ctypes.c_int(0), ctypes.c_int(100)) or 0
    g.call(A.STRING_DESTROY, native)
    g.call(A.NATIVE_FREE, native)
    if not parsed:
        raise RuntimeError('the engine could not parse the replay JSON')
    old = u64(manager + F('manager.replay'))
    ctypes.c_uint64.from_address(manager + F('manager.replay')).value = parsed
    if old:
        L.cfunc(u64(u64(old) + 8), None, ctypes.c_void_p)(old)       # release the previous replay (vtable[1])
    ctypes.c_int32.from_address(manager + F('manager.flag_54')).value = -1
    ctypes.c_int32.from_address(manager + F('manager.pending_type')).value = 4
    ctypes.c_int32.from_address(manager + F('manager.flag_10')).value = 3
    skip_flag(g)
    return parsed


def players(battle):
    logic = u64(battle + F('battle.logic')) if battle else 0
    return (u64(logic + F('logic.left_player')), u64(logic + F('logic.right_player'))) if logic else (0, 0)


def ensure_battlefield(g, battle, battle_json) -> int:
    data_tables = u64(battle + F('battle.data_tables'))
    key = engine_string(g, 'location')
    row = g.call(A.JSON_GET_DATA, battle_json, ctypes.addressof(key), data_tables, ctypes.c_int(-1), ctypes.c_bool(False)) or 0
    g.call(A.STRING_DESTROY, ctypes.addressof(key))
    if not row:
        raise RuntimeError('replay has no location row')
    field = g.call(A.LOCATION_BATTLEFIELD, row) or 0
    if field:
        return field
    primary, secondary = g.call(A.LOCATION_PRIMARY, row) or 0, g.call(A.LOCATION_SECONDARY, row) or 0
    names = [T.text(s) if s else None for s in (primary, secondary)]
    requests = (ctypes.c_uint8 * 64)()
    req = (ctypes.addressof(requests) + 15) & ~15
    g.call(A.RESOURCE_LIST_CTOR, req)
    for s in (primary, secondary):
        g.call(A.RESOURCE_LIST_APPEND, req, s, ctypes.c_int(-1), ctypes.c_int(-1), ctypes.c_int(-1), ctypes.c_int(-1),
               ctypes.c_bool(False))
    g.call(A.RESOURCE_LIST_LOAD, req)
    loaded = [g.call(A.RESOURCE_LOOKUP, ctypes.c_char_p(n.encode()), ctypes.c_int(0)) if n else None for n in names]
    print(f'  location resources {names} loaded {[hex(x or 0) for x in loaded]}', flush=True)
    resolved = [g.call(A.DATA_STRING_RESOLVE, s) if l else None for s, l in zip((primary, secondary), loaded)]
    root = g.call(A.BATTLEFIELD_CACHE_ROOT) or 0
    instance = g.call(A.DATA_ROW_INSTANCE_ID, row, restype=ctypes.c_int32)
    if all(resolved) and root and instance >= 0:
        g.call(A.BUILD_BATTLEFIELD_CACHE, root, resolved[0], resolved[1], ctypes.c_int(instance))
        field = g.call(A.LOCATION_BATTLEFIELD, row) or 0
    g.call(A.RESOURCE_LIST_DTOR, req)
    if not field:
        raise RuntimeError('the location battlefield could not be built')
    return field


def start_battle(g, manager, replay, max_pumps=4000):
    """Pump until the battle state exists and both players are loaded (nativePumpManager's battle section)."""
    loaded = False
    for pump in range(max_pumps):
        kind = i32(manager + F('manager.state_type'))
        E.pump(g, manager, 0.05 if kind != 4 else 0.0)
        time.sleep(0.001)
        kind, state = i32(manager + F('manager.state_type')), u64(manager + F('manager.state'))
        if kind != 4 or not state:
            continue
        for _ in range(10):
            battle = u64(state + F('state.battle'))
            left, right = players(battle)
            if (not left or not right) and battle and u64(battle + F('battle.logic')) and not loaded:
                key = engine_string(g, 'battle')
                battle_json = g.call(A.JSON_GET_OBJECT, replay, ctypes.addressof(key), ctypes.c_bool(True)) or 0
                g.call(A.STRING_DESTROY, ctypes.addressof(key))
                if battle_json:
                    print(f'  pump {pump}: battle 0x{battle:x}; battlefield 0x{ensure_battlefield(g, battle, battle_json):x}',
                          flush=True)
                    g.call(A.BATTLE_LOAD_REPLAY, battle, replay, 0, state + F('state.replay_target'))
                    loaded = True
                    left, right = players(battle)
            if not left or not right:
                g.call(A.BATTLE_CORE_UPDATE, state, ctypes.c_float(0.05))
                left, right = players(u64(state + F('state.battle')))
            if not left or not right:
                continue
            skip_flag(g)
            g.call(A.BATTLE_STATE_UPDATE, state, ctypes.c_float(0.05))
            return state, u64(state + F('state.battle')), pump
    raise RuntimeError(f'no battle after {max_pumps} pumps: state type {i32(manager + F("manager.state_type"))}')


def entities(battle):
    """Every entity in the battle registry: (kind, side, x, y, card id, hp, max hp); hp None when it has no health component.
    Layout from the sandbox (capture_episode_state / read_entity_hp)."""
    logic = u64(battle + F('battle.logic'))
    collection = u64(u64(logic + F('logic.registry')) + F('registry.collection'))
    data, count = u64(collection + F('collection.data')), i32(collection + F('collection.count'))
    out = []
    for i in range(count):
        e = u64(data + 8 * i)
        if not e:
            continue
        hp = maxhp = None
        comps = u64(e + F('entity.components'))
        if comps and u64(comps + 16):
            hp, maxhp = i32(u64(comps + 16) + F('health.hp')), i32(u64(comps + 16) + F('health.max'))
        out.append((i32(e + F('entity.kind')), i32(e + F('entity.side')), i32(e + F('entity.x')), i32(e + F('entity.y')),
                    i32(e + F('entity.card')), hp, maxhp))
    return out


def elixir(g, battle):
    return [g.call(A.PLAYER_ELIXIR, p, restype=ctypes.c_int32) for p in players(battle)]


def step(g, state, ticks):
    for _ in range(ticks):
        g.call(A.BATTLE_CORE_UPDATE, state, ctypes.c_float(0.05))
        skip_flag(g)
        g.call(A.BATTLE_STATE_UPDATE, state, ctypes.c_float(0.05))


def main() -> int:
    libdir, work, assets = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    ticks = int(sys.argv[4]) if len(sys.argv) > 4 else 100
    g, vm, info = boot.boot(libdir, work, assets)
    hooks.print_engine_errors(g)
    manager = C.g64(g, C.R['manager_global'])
    guards.install(g)
    for frame in range(3000):
        E.pump(g, manager, 0.05)
        time.sleep(0.001)
        if T.complete(T.tables_object(manager)):
            break
    print(f'  data tables complete at frame {frame}; manager state {i32(manager + F("manager.state_type"))}', flush=True)
    tables = T.tables_object(manager)
    probe = {name: T.character(tables, name).get('Hitpoints') for name in ('IceGolemite', 'HogRider', 'Musketeer')
             if name in T.rows(tables)}
    print(f'  hitpoints from the loaded data (level 1): {probe}', flush=True)
    replay_text = (Path(__file__).resolve().parent / 'replay_eight_card.json').read_text(encoding='utf-8')
    json.loads(replay_text)
    replay = load_replay(g, manager, replay_text)
    state, battle, pump = start_battle(g, manager, replay)
    left, right = players(battle)
    t0 = i32(battle + F('battle.tick'))
    print(f'  battle ready after {pump} pumps: state 0x{state:x} battle 0x{battle:x} players 0x{left:x} 0x{right:x} tick {t0}',
          flush=True)
    print(f'  elixir at tick {t0}: {elixir(g, battle)}', flush=True)
    began = time.perf_counter()
    step(g, state, ticks)
    spent = time.perf_counter() - began
    t1 = i32(battle + F('battle.tick'))
    same = u64(manager + F('manager.state')) == state and u64(state + F('state.battle')) == battle and i32(manager + F('manager.state_type')) == 4
    print(f'  after {ticks} steps: tick {t0} -> {t1}, same battle {same}, {spent * 1000 / ticks:.2f} ms per tick', flush=True)
    print(f'  elixir at tick {t1}: {elixir(g, battle)}', flush=True)
    found = entities(battle)
    towers = [e for e in found if e[0] in (12, 13) and e[4] == -1 and e[5] is not None]
    print(f'  {len(found)} entities; crown towers (kind, side, x, y, hp/max):', flush=True)
    for kind, side, x, y, _card, hp, mx in towers:
        print(f'    kind {kind} side {side} at ({x}, {y}) hp {hp}/{mx}', flush=True)
    princess = [t for t in towers if abs(t[2] - 9000) > 1500]
    ok = same and t1 - t0 == ticks and all(players(battle)) and len(princess) == 4
    print('GATE 3:', 'PASS' if ok else 'FAIL')
    return 0 if ok else 1


if __name__ == '__main__':
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
