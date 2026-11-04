"""Local battle interface used to calibrate, queue recorded commands and simulate replays.

    env = CrxEnv(libdir, workdir, assets)
    env.create_match(config)

Timing follows the probe (cr_replay_probe.cpp): a hand action for execute_tick N is applied while the battle is at tick N-1
and shows in the snapshot at tick N. Hero / champion abilities: queue_ability_action_at(AbilityAction(owner, -2, unit id))
sends the game's ability command (checked on both builds for Archer Queen, Golden Knight, Mighty Miner, Boss Bandit (both
charges), Monk, Little Prince, Skeleton King and the Musketeer hero: crx/test_unit_states.py); it raises only when that unit
has no ready ability (the probe's rule: button Ready or LimitedAvailability, no cooldown, a charge left).
"""
import ctypes
import json
import os
import time
from pathlib import Path

import bindings
import loader as L
import boot
import gate2c as C
import gate2e as E
import gate3 as G3
import gate4 as G4
import guards
import observe
import tables as T

DECISION_TICKS = 5
KING_IDS = {0: 5000000, 1: 5000003}
TOWER_OWNER = {5000000: 0, 5000001: 0, 5000002: 0, 5000003: 1, 5000004: 1, 5000005: 1}
MAX_TICKS = 9600            # a hard stop (8 minutes of game time) if the engine never reports the end
MIRROR = 28000006
MAIDEN = 28000025           # Spirit Empress: 6 (mounted) with 6+ elixir when played, else 3 (on foot); RoyaleAPI counts 6
ELIXIR_MAX = 100000         # raw elixir: 10,000 = 1
# raw elixir regenerated per tick, by the tick the step starts from (battle_timelines.csv "Default": 10 elixir per 28 s,
# 14 s from 2400, 9.3 s from 4800; measured: the first doubled gain is 2400 -> 2401)
REGEN = ((4800, ELIXIR_MAX / 186), (2400, ELIXIR_MAX / 280), (0, ELIXIR_MAX / 560))


class CrxEnv:
    def __init__(self, libdir, workdir, assets, template=None):
        os.environ.setdefault('CRX_ASSETS', str(assets))
        self.g, self.vm, _info = boot.boot(Path(libdir), Path(workdir), str(assets))
        self.B = bindings.of(self.g)
        self.manager = C.g64(self.g, self.B.glob('manager_global'))
        guards.install(self.g)
        for _frame in range(4000):
            E.pump(self.g, self.manager, 0.05)
            time.sleep(0.001)
            if T.complete(T.tables_object(self.manager)):
                break
        else:
            raise RuntimeError('the data tables did not finish loading')
        self.reader = observe.Reader(self.g)
        self.tables = T.tables_object(self.manager)    # the loaded data tables (Reader.effect_table)
        self.template = template
        self.state = self.battle = None
        self.scheduled = []           # (execute_tick, owner, hand_index, x, y) or (execute_tick, owner, 'ability', object id)
        self.held = []                # card plays by card id, held until playable: [execute_tick, owner, card, x, y, first]
        self.hold_log = []            # (first tick, owner, card, ticks held, 'played' | 'dropped') for every held play
        self.matches = 0
        self.ability_log = []         # (tick, owner, unit id, engine result) for every ability command
        self.inject_log = []          # (tick, command type, outcome) for every command handed to the engine
        self.stalled = False          # the core stopped advancing the tick: the battle is over (the sandbox's rule)
        # per owner: elixir spent (whole elixir, by card type) and leaked (raw), counted every tick from the elixir bar;
        # compared with RoyaleAPI's battle stats by the version oracle (il/engine_convert.fidelity)
        self.elixir = {0: {}, 1: {}}
        self._elixir_ptr, self._elixir_prev = None, None

    # ---- FirstLight-compatible calls -----------------------------------------------------------------------------------
    def wait_ready(self, timeout=15.0):
        return {'ok': True, 'ready': True}

    def close_transport(self):
        pass

    def create_match(self, config, *, template_path=None):
        if hasattr(config, 'to_replay_dict'):
            replay = config.to_replay_dict(template_path or self.template) if (template_path or self.template) \
                else config.to_replay_dict()
        elif isinstance(config, str):
            replay = json.loads(config)
        else:
            replay = dict(config)
        text = json.dumps(replay, separators=(',', ':'))
        if self.battle is None:
            parsed = G3.load_replay(self.g, self.manager, text)
            self.state, self.battle, _pump = G3.start_battle(self.g, self.manager, parsed)
        else:
            self._restart(text)
        self.scheduled = []
        self.held, self.hold_log = [], []
        self.stalled = False
        self.elixir = {0: {'spent': 0, 'leaked': 0.0, 'troop': 0, 'building': 0, 'spell': 0, 'other': 0, 'mirror': 0,
                           'cards': 0, 'maiden_normal': 0}, 1: None}
        self.elixir[1] = dict(self.elixir[0])
        self._elixir_ptr, self._elixir_prev = None, None
        self.matches += 1
        return self.observe()            # the opening state, as the probe returns it (deal calibration reads the hands)

    def _restart(self, text):
        """The sandbox's in-process 4 -> 4 replacement (jni_bridge.cpp nativeRestartReplay)."""
        g, B, m = self.g, self.B, self.manager
        native = g.call(B.fn('native_alloc'), 16)
        g.call(B.fn('string_from_utf8'), native, ctypes.c_char_p(text.encode()))
        G3.skip_flag(g)
        slot = m + B.field('manager.state')
        detached = ctypes.c_uint64.from_address(slot).value
        ctypes.c_uint64.from_address(slot).value = 0
        g.call(B.fn('set_replay_data'), m, native)
        ctypes.c_uint64.from_address(slot).value = detached
        G3.skip_flag(g)
        g.call(B.fn('manager_update'), m, ctypes.c_float(0.0))
        replay = ctypes.c_uint64.from_address(m + B.field('manager.replay')).value
        self.state, self.battle, _pump = G3.start_battle(g, m, replay)

    def tick(self):
        return G3.i32(self.battle + self.B.field('battle.tick'))

    def observe(self):
        objects = self.reader.objects(self.battle)
        crowns, ended, winner = self._result(objects)
        players = self.reader.players_state(self.battle)
        for p in players:
            p['accountId'] = (p['accountIdHigh'] << 32) | p['accountIdLow']
            p['crownsRaw'] = crowns[p['owner']]
        return {'ok': True, 'tick': self.tick(), 'players': players, 'objects': objects, 'ended': ended,
                'winner': winner, 'crownsRaw': [crowns[0], crowns[1]], 'logic': list(self.last_logic)}

    def queue_hand_action_at(self, action, *, execute_tick=None, execute_in_ticks=None, clamp_late=False):
        now = self.tick()
        target = int(execute_tick) if execute_tick is not None else now + int(execute_in_ticks or 1)
        if target <= now:
            raise RuntimeError('execute tick {} is not after the current tick {}'.format(target, now))
        self.scheduled.append((target, int(action.owner), int(action.hand_index), int(action.x), int(action.y)))
        return {'ok': True, 'tick': now, 'executeTick': target}

    HOLD_MAX = 60     # ticks a play may wait for its card or its elixir (the client accepts a tap ~1 elixir short)

    def queue_card_at(self, owner, card_id, x, y, *, execute_tick):
        """Play the card `card_id` (whichever hand slot holds it then) at `execute_tick`, or, as the live client does with a
        tap it cannot land yet, at the first tick after it when the card is in hand and affordable (at most HOLD_MAX ticks
        later). Replays record when a play landed; the engine refuses a play outright when the card is still on the refill
        timer or elixir is short, so queuing by slot at the exact tick lost such plays (clapha-engine docs/training/
        VERSION_ORACLE.md, 2026-10-12)."""
        now = self.tick()
        target = int(execute_tick)
        if target <= now:
            raise RuntimeError('execute tick {} is not after the current tick {}'.format(target, now))
        self.held.append([target, int(owner), int(card_id), int(x), int(y), target])
        return {'ok': True, 'tick': now, 'executeTick': target}

    def queue_ability_action_at(self, action, *, execute_in_ticks=1):
        """Activate the ability of one live hero/champion unit (AbilityAction(owner, -2, native object id))."""
        now = self.tick()
        object_id = int(action.secondary_index)
        owner = int(action.owner)
        if not any(o['nativeObjectId'] == object_id and o['owner'] == owner for o in self.reader.objects(self.battle)):
            raise RuntimeError('ability source {} is not a live unit of owner {}'.format(object_id, owner))
        # as the probe resolves it: a controller of this player must list the unit and show its ability ready (button 2)
        player = self.reader.player(self.reader.logic(self.battle), owner)
        rows = [a for a in self.reader.abilities(player) if object_id in a['championObjectIds']]
        if not rows:
            raise RuntimeError('unit {} has no ability controller'.format(object_id))
        # the probe's rule (cr_replay_probe.cpp resolve_ready_ability_request): button Ready (2) or LimitedAvailability (4),
        # no cooldown left, and a charge left (-1 = unlimited)
        if not any(a['buttonState'] in (2, 4) and a['remainingCooldownMs'] == 0 and a['remainingChargesRaw'] != 0 for a in rows):
            raise RuntimeError('ability of unit {} is not ready ({})'.format(
                object_id, [(a['buttonState'], a['remainingCooldownMs'], a['remainingChargesRaw']) for a in rows]))
        target = now + max(1, int(execute_in_ticks))
        self.scheduled.append((target, int(action.owner), 'ability', object_id, 0))
        return {'ok': True, 'tick': now, 'executeTick': target}

    def step(self, ticks=1):
        for _ in range(int(ticks)):
            self._advance()
        return {'ok': True, 'tick': self.tick()}

    def run_lean(self, until, every=DECISION_TICKS):
        """Advance to tick `until`; a lean snapshot at every multiple of `every` on the way. -> (frames, tick, ended)."""
        frames = []
        while self.tick() < until and not self.stalled:
            self._advance()
            tick = self.tick()
            crowns, ended, _winner = self._result()
            if tick % every == 0 or ended:
                frames.append(self.reader.frame(self.battle))
            if ended:
                return frames, tick, True
        if self.stalled:
            frames.append(self.reader.frame(self.battle))
            return frames, self.tick(), True
        return frames, self.tick(), False

    # ---- internals -----------------------------------------------------------------------------------------------------
    def _advance(self):
        now = self.tick()
        due = [s for s in self.scheduled if s[0] - 1 == now]
        self.scheduled = [s for s in self.scheduled if s[0] - 1 > now]
        for target, owner, kind, a, b in due:
            if kind == 'ability':
                self._queue_ability(owner, a, target)
            else:
                self._queue_card(owner, kind, a, b, target - 1)
        tried = []
        if self.held:
            players = {p['owner']: p for p in self.reader.players_state(self.battle)}
            for h in [h for h in self.held if h[0] - 1 == now]:
                target, owner, card, x, y, first = h
                slot = next((c for c in players[owner]['hand'] if c['cardId'] == card), None)
                if slot is not None and self._queue_card(owner, slot['handIndex'], x, y, target - 1):
                    tried.append((h, slot['deckSlot'], self._chosen_cost))
                else:
                    self._hold(h)
        before = self._elixir_read()
        G3.step(self.g, self.state, 1)
        spends = self._elixir_count(now, before)
        if tried:
            players = {p['owner']: p for p in self.reader.players_state(self.battle)}
            for h, deck_slot, cost in tried:
                if any(c['deckSlot'] == deck_slot for c in players[h[1]]['hand']):
                    self._hold(h)                               # refused (elixir short): try again next tick
                else:
                    self.held.remove(h)
                    self.hold_log.append((h[5], h[1], h[2], h[0] - h[5], 'played'))
                    # the card's cost from its selection, not from the elixir bar (an Elixir Collector or a dying Elixir
                    # Golem adds elixir in the same tick and made a play look cheaper)
                    stats = self.elixir[h[1]]
                    kind = {26: 'troop', 27: 'building', 28: 'spell'}.get(h[2] // 1000000, 'other')
                    stats[kind] += cost
                    stats['cards'] += cost
                    stats['mirror'] += h[2] == MIRROR           # costs one more than the card it copies
                    stats['maiden_normal'] += h[2] == MAIDEN and cost == 3
                    spends[h[1]] = spends.get(h[1], 0) - cost
        for owner, cost in spends.items():
            if cost > 0:
                self.elixir[owner]['other'] += cost             # spent beyond our card plays: ability presses
        if self.tick() == now and now >= 100:
            self.stalled = True

    def _elixir_read(self):
        if self._elixir_ptr is None:
            logic = self.reader.logic(self.battle)
            off = self.reader.f['player.elixir_raw']
            self._elixir_ptr = [self.reader.player(logic, o) + off for o in (0, 1)]
        return [ctypes.c_int32.from_address(p).value for p in self._elixir_ptr]

    def _elixir_count(self, start_tick, before):
        """After one tick: what each owner spent (the bar fell below what regeneration gives) and leaked (regeneration
        lost at a full bar). -> {owner: whole elixir spent this tick} for _advance to put under the played card's type."""
        if self.tick() == start_tick:
            return {}
        regen = next(r for t, r in REGEN if start_tick >= t)
        after = self._elixir_read()
        spends = {}
        for owner in (0, 1):
            prev, now = before[owner], after[owner]
            stats = self.elixir[owner]
            spent = prev + regen - now
            if spent >= 5000:                                   # a play (costs are whole elixir; regen is < 600)
                spends[owner] = round(spent / 10000)
                stats['spent'] += spends[owner]
            elif now >= ELIXIR_MAX:
                stats['leaked'] += max(0.0, prev + regen - ELIXIR_MAX)
        return spends

    def _hold(self, h):
        if h[0] + 1 - h[5] > self.HOLD_MAX:
            self.held.remove(h)
            self.hold_log.append((h[5], h[1], h[2], h[0] - h[5], 'dropped'))
        else:
            h[0] += 1

    # ---- the engine's own command queue (as the probe and the live game feed it) -----------------------------------
    def _inject(self, command: dict) -> bool:
        """Parse one command (the game's JSON form) with the engine's command parser and append it to the battle's command
        queue; the engine executes it inside its next tick, as a command from the server would be."""
        g, B = self.g, self.B
        text = json.dumps(command, separators=(',', ':'))
        native = g.call(B.fn('native_alloc'), 16)
        g.call(B.fn('string_from_utf8'), native, ctypes.c_char_p(text.encode()))
        parsed = g.call(B.fn('parse_json_object'), native, ctypes.c_int(0), ctypes.c_int(100)) or 0
        g.call(B.fn('string_destroy'), native)
        g.call(B.fn('native_free'), native)
        if not parsed:
            self.inject_log.append((self.tick(), command.get('ct'), 'json rejected'))
            return False
        out = (ctypes.c_uint64 * 1)()
        context = G3.u64(self.battle + B.field('battle.data_tables'))
        g.call(B.fn('logic_command_parse'), ctypes.addressof(out), parsed, context)
        L.cfunc(G3.u64(G3.u64(parsed) + 8), None, ctypes.c_void_p)(parsed)          # release the JSON object
        if not out[0]:
            self.inject_log.append((self.tick(), command.get('ct'), 'command parser rejected'))
            return False
        queue = G3.u64(self.battle + B.field('battle.command_queue'))
        self.inject_log.append((self.tick(), command.get('ct'), 'queued', G3.i32(queue + 0x14)))
        g.call(B.fn('logic_command_append'), queue, ctypes.addressof(out))                 # the queue takes ownership
        return True

    def _selection(self, owner, deck_slot):
        """(card id, card parameter) of a hand card: the engine's own selection builder (as the probe's read_card_state)."""
        g, B = self.g, self.B
        player = G4.player_of(g, self.battle, owner, self._account(owner))
        slot = G4.hand(g, player).get(deck_slot)
        if slot is None:
            return None
        entry = g.call(B.fn('hand_entry'), player, ctypes.c_int(slot))
        selection = (ctypes.c_uint8 * 64)()
        root = G3.u64(entry + 0x10)
        dynamic = root and G3.u64(root) == g.addr(B.glob('dynamic_choice_spell_vtable'))
        g.call(B.fn('build_dynamic_choice_selection' if dynamic else 'build_canonical_selection'),
               ctypes.addressof(selection), entry, player, ctypes.c_int(0))
        card_data = G3.u64(ctypes.addressof(selection))
        secondary = G3.u64(ctypes.addressof(selection) + 8)
        packed = ctypes.c_uint32.from_address(ctypes.addressof(selection) + 0x10).value
        card_id = G3.i32((secondary if secondary and (packed & 0xF) == 0 else card_data) + B.field('data.id'))
        return card_id, packed

    def _account(self, owner):
        p = {q['owner']: q for q in self.reader.players_state(self.battle)}[owner]
        return p['accountIdHigh'], p['accountIdLow']

    def _queue_card(self, owner, hand_index, x, y, boundary):
        players = {p['owner']: p for p in self.reader.players_state(self.battle)}
        card = next((h for h in players[owner]['hand'] if h['handIndex'] == hand_index), None)
        if card is None:
            return False
        chosen = self._selection(owner, card['deckSlot'])
        if chosen is None:
            return False
        hi, lo = self._account(owner)
        self._chosen_cost = (chosen[1] >> 28) & 0xF          # the cost the selection carries (Spirit Empress: 3 or 6)
        return self._inject({'ct': self.B.data['command_types']['play_card'], 'c': {'t': boundary - 20, 't2': boundary, 'idHi': hi, 'idLo': lo, 'px': x, 'py': y,
                                             'sid': -1, 'sel': {'os': chosen[0], 'pd': chosen[1]}}})

    def _queue_ability(self, owner, object_id, target):
        hi, lo = self._account(owner)
        ok = self._inject({'ct': self.B.data['command_types']['ability'], 'c': {'t': target - 2, 't2': target - 1, 'idHi': hi, 'idLo': lo, 'cgid': object_id}})
        self.ability_log.append((self.tick(), owner, object_id, 'queued' if ok else 'parse failed'))
        return ok

    def _play_hand(self, owner, hand_index, x, y):
        players = {p['owner']: p for p in self.reader.players_state(self.battle)}
        card = next((h for h in players[owner]['hand'] if h['handIndex'] == hand_index), None)
        if card is None:
            return {'accepted': False, 'reason': 'card_not_in_hand'}
        account = (players[owner]['accountIdHigh'], players[owner]['accountIdLow'])
        return G4.play(self.g, self.battle, owner, card['deckSlot'], x, y, account=account)

    def _use_ability(self, owner, object_id):
        """The sandbox's nativeUseAbility: AbilityCommand(context, unit), the player's account, execute(command, battle, 3, 0)."""
        g, B = self.g, self.B
        unit = next((p for p in self.reader.object_pointers(self.battle)
                     if p and G3.i32(p + B.field('entity.id')) == object_id), 0)
        if not unit:
            return {'accepted': False, 'reason': 'unit_gone'}
        player = {p['owner']: p for p in self.reader.players_state(self.battle)}[owner]
        context = G3.u64(self.battle + B.field('battle.data_tables'))
        command = g.call(B.fn('native_alloc'), 0x38)
        g.call(B.fn('ability_command_ctor'), command, context, unit)
        ctypes.c_int32.from_address(command + 0x14).value = player['accountIdHigh']
        ctypes.c_int32.from_address(command + 0x18).value = player['accountIdLow']
        result = g.call(B.fn('ability_command_execute'), command, self.battle, ctypes.c_int(3), ctypes.c_int(0),
                        restype=ctypes.c_int32)
        L.cfunc(G3.u64(G3.u64(command) + 8), None, ctypes.c_void_p)(command)
        self.last_ability = {'accepted': result == 0, 'result': result}
        self.ability_log.append((self.tick(), owner, object_id, result))
        return self.last_ability

    def _result(self, objects=None):
        """Crowns from the crown towers standing; ended when a King Tower falls or the engine's logic reaches its end."""
        objects = objects if objects is not None else self.reader.tower_hp(self.battle)    # only towers are looked at
        standing = {o['nativeObjectId'] for o in objects if o['nativeObjectId'] in TOWER_OWNER and (o['hp'] or 0) > 0}
        crowns = {0: 0, 1: 0}
        for tower, owner in TOWER_OWNER.items():
            if tower not in standing:
                crowns[1 - owner] += 3 if tower in KING_IDS.values() else 1
        crowns = {k: min(3, v) for k, v in crowns.items()}
        logic = self.reader.logic(self.battle)
        substate_ptr = ctypes.c_uint64.from_address(logic + self.B.field('logic.substate')).value
        substate = G3.i32(substate_ptr + self.B.field('substate.value')) if substate_ptr else -1
        logic_state = G3.i32(logic + self.B.field('logic.state'))
        # the engine's own result, as the probe reads it (read_battle_result): finalized flag, and the winning owner
        finalized = ctypes.c_uint8.from_address(logic + self.B.field('logic.finalized')).value != 0
        world_result = G3.i32(logic + self.B.field('logic.result'))
        self.engine_result = (finalized, world_result)
        ended = (finalized or self.stalled or KING_IDS[0] not in standing or KING_IDS[1] not in standing
                 or (logic_state == 1 and substate in (6, 15)) or self.tick() >= MAX_TICKS)
        self.last_logic = (logic_state, substate)
        winner = None
        if finalized and world_result in (0, 1):
            winner = world_result
        elif ended and crowns[0] != crowns[1]:
            winner = 0 if crowns[0] > crowns[1] else 1
        return crowns, ended, winner
