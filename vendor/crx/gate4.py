"""GATE 4 (first part): a card is played and the battle reacts. After gate 3's battle, the sandbox's command path
(jni_bridge.cpp nativeAct): find the player by account id, deck slot -> hand slot -> hand entry, build a DoSpellCommand
(account, x, y, and the card selection built from the hand entry), check the spot with the engine's own deployment validator,
execute it against the battle, then tick.

Passes when: the command is accepted (result 0), the player's elixir drops by the card's cost, a unit with that card id
appears and moves, and an enemy crown tower loses hitpoints within the run.

    python3 tools/crx/gate4.py LIBDIR WORKDIR ASSETS [ticks]
"""
import ctypes
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
import gate3 as G3  # noqa: E402
import guards  # noqa: E402
import hooks  # noqa: E402
import tables as T  # noqa: E402

i32, u64, F = G3.i32, G3.u64, G3.F
CARD_NAMES = {26000000: 'Knight', 26000001: 'Archers', 26000003: 'Giant', 26000010: 'Skeletons', 26000014: 'Musketeer',
              26000021: 'HogRider', 27000000: 'Cannon', 28000001: 'Arrows'}
PREFERRED = (26000021, 26000003, 26000000, 26000014)          # a troop that walks to a tower: Hog Rider first
ACCOUNTS = {0: (1, 1), 1: (2, 2)}                              # the replay's avatar0 / avatar1 account ids


def card_at(replay_text, side, deck_index):
    import json
    return int(json.loads(replay_text)['battle']['deck%d' % side]['sp'][deck_index]['d'])


def fn(name):
    return bindings.of().fn(name)


def player_of(g, battle, side, account=None):
    logic = u64(battle + F('battle.logic'))
    hi, lo = account or ACCOUNTS[side]
    index = g.call(fn('battle_identity_index'), logic, ctypes.c_int(hi), ctypes.c_int(lo), restype=ctypes.c_int32)
    return g.call(fn('battle_player_at_index'), logic, ctypes.c_int(index)) if index >= 0 else None


def hand(g, player):
    """deck slot -> hand slot, for the cards in hand."""
    out = {}
    for deck_index in range(8):
        slot = g.call(fn('deck_index_to_hand'), player, ctypes.c_int(deck_index), restype=ctypes.c_int32)
        if 0 <= slot <= 3:
            out[deck_index] = slot
    return out


def play(g, battle, side, deck_index, x, y, dry_run=False, account=None):
    """The sandbox's nativeAct. Returns a dict: placement code, result code, cost."""
    B = bindings.of()
    logic = u64(battle + F('battle.logic'))
    context = u64(battle + F('battle.data_tables'))            # the command context (battle +0x208 in 150535029)
    player = player_of(g, battle, side, account)
    slot = hand(g, player).get(deck_index)
    if slot is None:
        return {'accepted': False, 'reason': 'card_not_in_hand'}
    entry = g.call(fn('hand_entry'), player, ctypes.c_int(slot))
    command = g.call(fn('native_alloc'), 0x58)
    g.call(fn('do_spell_command_ctor'), command, context)
    hi, lo = account or ACCOUNTS[side]
    for off, value in ((0x14, hi), (0x18, lo), (0x28, x), (0x2C, y), (0x30, -1)):
        ctypes.c_int32.from_address(command + off).value = value
    root = u64(entry + 0x10)
    dynamic = root and u64(root) == g.addr(B.glob('dynamic_choice_spell_vtable'))
    g.call(fn('build_dynamic_choice_selection' if dynamic else 'build_canonical_selection'),
           command + 0x38, entry, player, ctypes.c_int(0))
    cost = (ctypes.c_uint32.from_address(command + 0x48).value >> 28) & 0xF
    resolved = g.call(fn('resolve_canonical_selection'), command + 0x38)
    placement = g.call(fn('validate_deployment'), logic, ctypes.c_int(x), ctypes.c_int(y), resolved, ctypes.c_int(0),
                       restype=ctypes.c_int32) if resolved else -1
    result = None
    if not dry_run and placement == 0:
        result = g.call(fn('do_spell_command_execute'), command, battle, ctypes.c_int(3), ctypes.c_int(0),
                        restype=ctypes.c_int32)
    L.cfunc(u64(u64(command) + 8), None, ctypes.c_void_p)(command)       # release (vtable[1])
    return {'accepted': result == 0, 'placement': placement, 'result': result, 'cost': cost, 'hand_slot': slot}


def units(battle):
    return [e for e in G3.entities(battle) if e[4] not in (-1, 0)]


def main() -> int:
    libdir, work, assets = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    ticks = int(sys.argv[4]) if len(sys.argv) > 4 else 600
    g, vm, info = boot.boot(libdir, work, assets)
    hooks.print_engine_errors(g)
    manager = C.g64(g, bindings.of().glob('manager_global'))
    guards.install(g)
    for frame in range(3000):
        E.pump(g, manager, 0.05)
        time.sleep(0.001)
        if T.complete(T.tables_object(manager)):
            break
    replay_text = (Path(__file__).resolve().parent / 'replay_eight_card.json').read_text(encoding='utf-8')
    replay = G3.load_replay(g, manager, replay_text)
    state, battle, _pump = G3.start_battle(g, manager, replay)
    G3.step(g, state, 20)                                        # 1 s in
    player = player_of(g, battle, 0)
    in_hand = hand(g, player)
    names = {d: CARD_NAMES.get(card_at(replay_text, 0, d), '?') for d in in_hand}
    print('  blue hand (deck slot -> hand slot): {}  {}'.format(in_hand, names), flush=True)
    pick = next((d for c in PREFERRED for d in in_hand if card_at(replay_text, 0, d) == c), None)
    if pick is None:
        print('GATE 4: FAIL (no troop in hand)')
        return 1
    card = card_at(replay_text, 0, pick)
    x, y = 14000, 13000                                          # blue's right lane, own half (blue King Tower at y 3000)
    logic = u64(battle + F('battle.logic'))
    waited = 0
    while g.call(fn('battle_commands_held'), logic, restype=ctypes.c_bool) and waited < 400:   # the start-of-battle phase
        G3.step(g, state, 1)
        waited += 1
    print('  commands accepted from tick {} (held for the first {} ticks after tick 20)'.format(
        i32(battle + F('battle.tick')), waited), flush=True)
    elixir_before = G3.elixir(g, battle)[0]
    towers_before = {(t[1], t[2], t[3]): t[5] for t in G3.entities(battle) if t[0] in (12, 13) and t[4] == -1}
    check = play(g, battle, 0, pick, x, y, dry_run=True)
    print('  dry run: {} at ({}, {}): {}'.format(CARD_NAMES.get(card), x, y, check), flush=True)
    done = play(g, battle, 0, pick, x, y)
    elixir_after = G3.elixir(g, battle)[0]
    print('  played {}: {}; blue elixir {} -> {}'.format(CARD_NAMES.get(card), done, elixir_before, elixir_after), flush=True)
    track, hits = [], {}
    for t in range(ticks):
        G3.step(g, state, 1)
        if t % 20 == 0 or t == ticks - 1:
            mine = [u for u in units(battle) if u[4] == card and u[1] == 0]
            if mine:
                track.append((t, mine[0][2], mine[0][3], mine[0][5]))
            for tw in G3.entities(battle):
                if tw[0] in (12, 13) and tw[4] == -1 and tw[1] == 1:
                    before = towers_before.get((tw[1], tw[2], tw[3]))
                    if before is not None and tw[5] is not None and tw[5] < before:
                        hits[(tw[2], tw[3])] = (before, tw[5])
    for t, ux, uy, hp in track[:12]:
        print('    tick +{:3d}: {} at ({}, {}) hp {}'.format(t, CARD_NAMES.get(card), ux, uy, hp), flush=True)
    print('  enemy towers damaged: {}'.format(hits or 'none'), flush=True)
    moved = len(track) >= 2 and abs(track[-1][2] - track[0][2]) + abs(track[-1][1] - track[0][1]) > 2000
    ok = bool(done.get('accepted')) and elixir_before - elixir_after == done['cost'] and bool(track) and moved and bool(hits)
    why = 'accepted {}, elixir {}->{} cost {}, unit seen {}, moved {}, tower hit {}'.format(
        done.get('accepted'), elixir_before, elixir_after, done.get('cost'), bool(track), moved, bool(hits))
    print('GATE 4: ' + ('PASS' if ok else 'FAIL (' + why + ')'))
    return 0 if ok else 1


if __name__ == '__main__':
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
