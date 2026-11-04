"""Battle snapshots in the format FirstLight's probe gives (native_runner/probe/cr_replay_probe.cpp, observe-lean / run-lean),
read straight from engine memory for replay preparation and rendered-state verification.

Layouts follow the probe's documented sources (LogicGameObject+0x08 id, +0x78 owner, +0xAC card, +0x7C/+0x80 x/y, component
type 0 = attack (+0x10 target, +0x20 stage, +0x24 timeline, +0x28 load), type 1 = movement (+0x1E0 charge), type 2 = health
(+0x10 hp, +0x14 max); world +0xE0/+0x30/+0x88 per owner = player / identity / deck; player +0x2F8 elixir, +0x220 hand,
+0x230 cycle, +0x2E8 evolution progress). Every offset comes from the build's bindings ("fields"); `check()` verifies them
against the engine's own functions on the loaded build.
"""
import ctypes
import struct

import bindings

TOWER_IDS = range(5000000, 5000006)
BURST_SLOT = 10                 # Dagger Duchess: the burst runner in the tower's action array (see unit_states)


def _u64(a):
    return ctypes.c_uint64.from_address(a).value


def _i32(a):
    return ctypes.c_int32.from_address(a).value


class _Block:
    """Several fields of one structure in one read: fields = [(offset, 'i' int32 | 'Q' uint64)], read(address) -> their
    values in the given order. Only for fields the caller already reads one by one (the span between them is inside the
    same structure)."""

    def __init__(self, fields):
        self.start = min(o for o, _ in fields)
        order = sorted(range(len(fields)), key=lambda i: fields[i][0])
        fmt, at = '<', self.start
        for i in order:
            o, kind = fields[i]
            fmt += 'x' * (o - at) + kind
            at = o + struct.calcsize('<' + kind)
        self.size, self.unpack = at - self.start, struct.Struct(fmt).unpack
        self.back = [order.index(i) for i in range(len(fields))]

    def read(self, address):
        values = self.unpack(ctypes.string_at(address + self.start, self.size))
        return [values[i] for i in self.back]


class Reader:
    def __init__(self, g):
        self.g = g
        B = bindings.of(g)
        self.f = {k: int(v, 16) for k, v in B.data['fields'].items()}
        # static game-data rows (names, spawns, hit speeds, effect percentages) and engine predicates, read once: the data
        # tables are loaded once per process and never change
        self._static, self._names, self._predicates, self._blocks = {}, {}, {}, None

    # ---- containers ------------------------------------------------------------------------------------------------
    def vector(self, obj, off, limit=64):
        """A native vector {data, capacity@+8, count@+12}: (data pointer, count)."""
        data, cap, count = _u64(obj + off), _i32(obj + off + 8), _i32(obj + off + 12)
        if count < 0 or cap < count or cap > limit or (count and not data):
            raise ValueError('bad vector at +0x{:x}'.format(off))
        return data, count

    def logic(self, battle):
        return _u64(battle + self.f['battle.logic'])

    def player(self, logic, owner):
        return _u64(logic + self.f['logic.player'] + 8 * owner)

    def deck_slots(self, logic, owner):
        deck = _u64(logic + self.f['logic.deck'] + 8 * owner)
        data, count = self.vector(deck, self.f['deck.slots'], 8)
        return [_u64(data + 8 * i) for i in range(count)]

    def card_data(self, slot):
        return _u64(slot + self.f['deckslot.card_data'])

    def card_id(self, data):
        return _i32(data + self.f['data.id'])

    # ---- players -----------------------------------------------------------------------------------------------------
    def players_state(self, battle):
        logic = self.logic(battle)
        out = []
        for owner in (0, 1):
            player = self.player(logic, owner)
            identity = _u64(logic + self.f['logic.identity'] + 8 * owner)
            slots = self.deck_slots(logic, owner)
            ids = [self.card_id(self.card_data(s)) for s in slots]
            hand_data, hand_count = self.vector(player, self.f['player.hand'], 8)
            cycle_data, cycle_count = self.vector(player, self.f['player.cycle'], 8)
            hand = []
            for index in range(hand_count):
                slot = _i32(hand_data + 4 * index)
                if slot >= 0:
                    hand.append({'handIndex': index, 'deckSlot': slot, 'cardId': ids[slot]})
            cycle = [{'cycleIndex': i, 'deckSlot': _i32(cycle_data + 4 * i), 'cardId': ids[_i32(cycle_data + 4 * i)]}
                     for i in range(cycle_count)]
            raw = _i32(player + self.f['player.elixir_raw'])
            out.append({'owner': owner, 'accountIdHigh': _i32(identity + self.f['identity.account_hi']) & 0xFFFFFFFF,
                        'accountIdLow': _i32(identity + self.f['identity.account_lo']) & 0xFFFFFFFF,
                        'elixirRaw': raw, 'elixir': raw / 10000.0, 'hand': hand, 'cycle': cycle,
                        'nextCard': {'deckSlot': cycle[0]['deckSlot'], 'cardId': cycle[0]['cardId']} if cycle else None,
                        'deck': [{'deckSlot': i, 'cardId': c} for i, c in enumerate(ids)]})
        return out

    def text(self, a):
        """An engine string (length at +4; inline under 8 bytes, else a pointer at +8)."""
        n = _i32(a + 4)
        if not 0 < n <= 128:
            return ''
        src = a + 8 if n < 8 else _u64(a + 8)
        return ctypes.string_at(src, n).decode('utf-8', 'replace') if src else ''

    def abilities(self, player):
        """The hero/champion controllers of one player (the probe's abilityRuntime): slot 1 and 2."""
        f = self.f
        rows = []
        for index in range(2):
            controller = _u64(player + f['player.ability_controllers'] + 8 * index)
            if not controller:
                continue
            selected = _u64(controller + f['controller.selected_character'])
            ability = _u64(selected + f['character.ability_data']) if selected else 0
            action = _u64(controller + f['controller.action_data'])
            name = self.text(ability + f['named.name']) if ability else (self.text(action + f['named.name']) if action else '')
            champions = []
            vector = _u64(controller + f['controller.champions'])
            if vector:
                data, count = _u64(vector), _i32(vector + 12)
                if 0 <= count <= 64 and (data or not count):
                    champions = [_i32(_u64(data + 8 * k) + f['entity.id']) & 0xFFFFFFFF for k in range(count) if _u64(data + 8 * k)]
            rows.append({'controllerSlot': index + 1, 'actionDataName': name, 'championObjectIds': champions,
                         'remainingCooldownMs': _i32(controller + f['controller.remaining_cooldown']),
                         'configuredCooldownMs': _i32(controller + f['controller.configured_cooldown']),
                         'remainingChargesRaw': _i32(controller + f['controller.charges']),
                         'maxCharges': _i32(ability + f['abilitydata.max_charges']) if ability else None,
                         'buttonState': _i32(controller + f['controller.button_state']),
                         'available': bool(selected)})
        return rows

    def players_lean(self, battle):
        """Per owner: evolution progress per deck slot (the probe's evolutionRuntime) and the hero/champion controllers
        (abilityRuntime)."""
        logic = self.logic(battle)
        out = []
        for owner in (0, 1):
            player = self.player(logic, owner)
            slots = self.deck_slots(logic, owner)
            prog_data, prog_count = self.vector(player, self.f['player.evo_progress'], 8)
            evolutions = []
            for slot_index, slot in enumerate(slots):
                data = self.card_data(slot)
                progress = _i32(prog_data + 4 * slot_index) if slot_index < prog_count else 0
                row = {'deckSlot': slot_index, 'cardId': self.card_id(data), 'baseSpellGlobalId': self.card_id(data),
                       'evolvable': False, 'evolutionFormGlobalId': None, 'progress': progress,
                       'cycleRequired': None, 'cycleRemaining': None, 'ready': None}
                cand_data, cand_count = self.vector(data, self.f['carddata.form_candidates'], 16)
                for k in range(cand_count):
                    form = _u64(cand_data + 8 * k)
                    meta = _u64(form + self.f['form.metadata'])
                    if meta and _i32(meta + self.f['formmeta.code']) == 1:
                        need = _i32(form + self.f['form.cycle_required'])
                        row.update(evolvable=True, evolutionFormGlobalId=_i32(form + self.f['data.id']),
                                   cycleRequired=need, cycleRemaining=max(0, need - progress), ready=progress >= need)
                evolutions.append(row)
            out.append({'owner': owner, 'abilityRuntime': self.abilities(player), 'evolutionRuntime': evolutions})
        return out

    # ---- objects -----------------------------------------------------------------------------------------------------
    def object_pointers(self, battle):
        f = self.f
        collection = _u64(_u64(self.logic(battle) + f['logic.registry']) + f['registry.collection'])
        data, count = _u64(collection + f['collection.data']), _i32(collection + f['collection.count'])
        return [_u64(data + 8 * i) for i in range(count)]

    def component(self, entity, kind):
        f = self.f
        if _i32(entity + f['entity.component_count']) <= kind:
            return 0
        return _u64(_u64(entity + f['entity.components']) + 8 * kind)

    def tower_hp(self, battle):
        """[{'nativeObjectId', 'hp'}] for the six towers still in the battle: the two fields CrxEnv._result needs from
        objects(), read alone (objects() reads every field of every object; _result runs every tick)."""
        f = self.f
        out = []
        for e in self.object_pointers(battle):
            if not e:
                continue
            oid = _i32(e + f['entity.id']) & 0xFFFFFFFF
            if 5000000 <= oid < 5000006:
                health = self.component(e, 2)
                out.append({'nativeObjectId': oid, 'hp': _i32(health + f['health.hp']) if health else None})
        return out

    def objects(self, battle):
        f = self.f
        pointers = self.object_pointers(battle)
        ids = {p: _i32(p + f['entity.id']) & 0xFFFFFFFF for p in pointers if p}
        out = []
        for slot, e in enumerate(pointers):
            if not e:
                continue
            owner = _i32(e + f['entity.side'])
            health = self.component(e, 2)
            attack = self.component(e, 0)
            movement = self.component(e, 1)
            data = _u64(e + f['entity.data'])
            target = _u64(attack + f['attack.target']) if attack else 0
            target_key = [_i32(target + f['entity.side']), -2, ids[target]] if target in ids else None
            phase = {'attackValidated': bool(attack), 'movementValidated': False,
                     'attackSequenceStage': _i32(attack + f['attack.stage']) if attack else None,
                     'attackTimelineMs': _i32(attack + f['attack.timeline']) if attack else None,
                     'loadRemainingMs': _i32(attack + f['attack.load']) if attack else None,
                     # as the probe (phase_runtime_telemetry.inc): only for objects with an attack component; others
                     # are smaller (the 3000000 ids keep their own timer at 0x100) and 0x15C lies past their end
                     'deployRemainingMs': _i32(e + f['entity.deploy_remaining']) if attack else None,
                     'classicChargeProgress': None}
            if movement:
                charge = _i32(movement + f['movement.charge'])
                if charge >= -1:
                    phase.update(movementValidated=True, classicChargeProgress=charge)
            out.append({'slot': slot, 'nativeObjectId': ids[e], 'entityKey': [owner, -2, ids[e]], 'owner': owner,
                        'cardId': _i32(e + f['entity.card']),
                        'dataGlobalId': (_i32(data + f['entitydata.global_id']) & 0xFFFFFFFF) if data else 0,
                        'x': _i32(e + f['entity.x']), 'y': _i32(e + f['entity.y']),
                        'targetX': _i32(e + f['entity.target_x']), 'targetY': _i32(e + f['entity.target_y']),
                        'targetEntityKey': target_key, 'targetEntityValidated': True,
                        'hp': _i32(health + f['health.hp']) if health else None,
                        'maxHp': _i32(health + f['health.max']) if health else None,
                        'attackSequenceStage': phase['attackSequenceStage'], 'phaseRuntime': phase})
        return out

    def entities(self, battle):
        """The fields of objects() that a lean State needs, one tuple per object, without objects()' dicts:
        (native id, owner, card id, x, y, hp, max hp, target's native id, attack load ms, deploy remaining ms,
        data global id). hp / max hp are None without a health component; target, load and deploy are None without an
        attack component (target also when it is not a listed object), as objects() reports them."""
        f = self.f
        f_id, f_side, f_card, f_x, f_y = f['entity.id'], f['entity.side'], f['entity.card'], f['entity.x'], f['entity.y']
        f_count, f_comps, f_data, f_global = f['entity.component_count'], f['entity.components'], f['entity.data'], f['entitydata.global_id']
        f_target, f_load, f_deploy = f['attack.target'], f['attack.load'], f['entity.deploy_remaining']
        f_hp, f_max = f['health.hp'], f['health.max']
        pointers = self.object_pointers(battle)
        ids = {p: _i32(p + f_id) & 0xFFFFFFFF for p in pointers if p}
        out = []
        for e in pointers:
            if not e:
                continue
            count = _i32(e + f_count)
            comps = _u64(e + f_comps) if count > 0 else 0
            attack = _u64(comps) if count > 0 else 0
            health = _u64(comps + 16) if count > 2 else 0
            data = _u64(e + f_data)
            target = _u64(attack + f_target) if attack else 0
            out.append((ids[e], _i32(e + f_side), _i32(e + f_card), _i32(e + f_x), _i32(e + f_y),
                        _i32(health + f_hp) if health else None, _i32(health + f_max) if health else None,
                        ids[target] if attack and target in ids else None,
                        _i32(attack + f_load) if attack else None, _i32(e + f_deploy) if attack else None,
                        (_i32(data + f_global) & 0xFFFFFFFF) if data else 0))
        return out

    # ---- unit states (checked card by card on the engine: docs/engine/UNIT_STATES.md) -------------------------------------
    # LogicCharacter state (+0x11C), as observed per card; other values stay unnamed until checked
    STATE_NAMES = {0: 'idle', 1: 'moving', 2: 'attacking', 3: 'dashing', 4: 'deploying', 5: 'river_jump', 6: 'underground',
                   7: 'ability_underground', 10: 'ability_cast', 11: 'waiting_to_appear', 12: 'pulled',
                   13: 'reeling_to_building', 14: 'hooking', 16: 'deflecting'}
    # buffs that champion abilities put on their own unit (checked: Archer Queen, Boss Bandit, Monk, Golden Knight)
    ABILITY_BUFFS = ('ArcherQueenRapid', 'BossBandit_ability_buff', 'ShieldBoostMonk', 'GoldenKnightCharge')

    def _engine_bool(self, address, *args):
        """Call an engine predicate (absolute address) on this build; its bool comes back in al."""
        return bool(self.g.call(address - self.g.base, *args, restype=ctypes.c_uint8) & 1)

    def _predicate(self, address, immune=False):
        """A callable for an engine predicate at an absolute address: fn(entity) (immune: fn(entity, 1)) -> int whose bit 0 is
        the answer. Built once per address (the per-call ctypes prototype was most of unit_states()' time); Windows goes
        through the loader's own call."""
        fn = self._predicates.get(address)
        if fn is None:
            import loader
            if loader.WINDOWS:
                if immune:
                    fn = lambda e, a=address: self.g.call(a - self.g.base, e, ctypes.c_int(1), restype=ctypes.c_uint8)
                else:
                    fn = lambda e, a=address: self.g.call(a - self.g.base, e, restype=ctypes.c_uint8)
            else:
                proto = ctypes.CFUNCTYPE(ctypes.c_uint8, ctypes.c_void_p, *((ctypes.c_int,) if immune else ()))(address)
                fn = (lambda e, p=proto: p(e, 1)) if immune else proto
            self._predicates[address] = fn
        return fn

    def _buff_data(self, asset):
        """An effect's data row (static game data): (name, global id, hit speed %, speed %, spawn speed %), cached per row."""
        row = self._static.get(asset)
        if row is None:
            f = self.f
            row = self._static[asset] = (self.text(asset + f['named.name']), _i32(asset + f['data.id']) & 0xFFFFFFFF,
                                         _i32(asset + f['buffdata.hit_speed']), _i32(asset + f['buffdata.speed']),
                                         _i32(asset + f['buffdata.spawn_speed']))
        return row

    def _unit_data(self, data):
        """A unit's data row (static game data): (name, spawns or None, leavesOnDeath, hit speed, load time), cached per row."""
        row = self._static.get(data)
        if row is None:
            f = self.f
            spawn = _u64(data + f['characterdata.spawn_character'])
            left = [_u64(data + f['characterdata.death_spawn']), _u64(data + f['characterdata.death_projectile'])]
            row = self._static[data] = (self.text(data + f['named.name']), self.text(spawn + f['named.name']) if spawn else None,
                                        tuple(self.text(a + f['named.name']) for a in left if a),
                                        _i32(data + f['characterdata.hit_speed']), _i32(data + f['characterdata.load_time']))
        return row

    def _character_flags(self, data):
        """A character's data row for the three checks below (static game data, cached): (folds away, hide time, state 7
        stays targetable, dash immunity, flying height)."""
        key = ('checks', data)
        row = self._static.get(key)
        if row is None:
            f = self.f
            row = self._static[key] = (
                bool(ctypes.c_uint8.from_address(data + f['characterdata.folds_a']).value
                     or ctypes.c_uint8.from_address(data + f['characterdata.folds_b']).value),
                _i32(data + f['characterdata.hide_time']),
                ctypes.c_uint8.from_address(data + f['characterdata.burrow_targetable']).value != 0,
                _i32(data + f['characterdata.dash_immunity']), _i32(data + f['characterdata.flying_height']))
        return row

    def _checks(self, e, state, data):
        """(hidden, immune, airborne) from plain fields, as the engine's own functions decide them (read from their code on
        both builds, 2026-10-12: character vtable +0xF8 / +0x100 / +0x108 and character_damage_immune; docs/engine/
        UNIT_STATES.md). The same values as calling them (checked on every unit of many games), but readable without running
        engine code: the live reader (the game on a phone / emulator) can only read memory."""
        f = self.f
        folds, hide_time, keeps_targetable, dash_immunity, flying = self._character_flags(data)
        status = _u64(e + f['character.status'])
        if state == 11 or state == 6 or (state == 7 and not keeps_targetable):
            hidden = True
        elif folds:
            hidden = _i32(e + f['character.fold_timer']) == hide_time
        else:
            hidden = bool(status >> f['status.hidden_bit'] & 1)
        immune = _u64(e + f['character.immune_ref']) != 0 or (state == 3 and dash_immunity > 0) \
            or _i32(e + f['character.immune_timer']) > 0
        air, ground = status >> f['status.air_bit'] & 1, status >> f['status.ground_bit'] & 1
        if air and ground:
            airborne = _i32(e + f['character.air_count_a']) + _i32(e + f['character.air_count_b']) > 0
        else:
            airborne = bool(air) or (not ground and flying > 0)
        return hidden, immune, airborne

    def _name(self, data):
        name = self._names.get(data)
        if name is None:
            name = self._names[data] = self.text(data + self.f['named.name'])
        return name

    def buffs(self, entity, ids=None):
        """Active buffs of one unit (its type-3 component): [{name, globalId, remainingMs, elapsedMs, sourceOwner,
        sourceObjectId, hitSpeedPercent, speedPercent, spawnSpeedPercent}], plus the component's invisible counter.
        stopsMovement / stopsAttacks: the effect's speed / hit speed is -100 % (Freeze, stuns, Vines, nets, carried by an
        evolved Snowball, ...). ([], 0) without a buff component. elapsedMs = time since the buff was applied (its total so far minus the time
        left); sourceObjectId = the object that applied it (the unit itself for self buffs, a spell area, ...) when `ids`
        (object pointer -> id) lists it, else None."""
        f = self.f
        comp = self.component(entity, 3)
        if not comp:
            return [], 0
        data, count = _u64(comp + f['buff.entries']), _i32(comp + f['buff.count'])
        out = []
        f_data, f_left, f_total, f_side, f_source = (f['buffentry.data'], f['buffentry.remaining'], f['buffentry.total'],
                                                     f['buffentry.source_side'], f['buffentry.source'])
        for k in range(max(0, min(count, 32))):
            entry = _u64(data + 8 * k) if data else 0
            asset = _u64(entry + f_data) if entry else 0
            if not asset:
                continue
            name, gid, hit, speed, spawn = self._buff_data(asset)
            left = _i32(entry + f_left)
            out.append({'name': name, 'globalId': gid, 'remainingMs': left, 'elapsedMs': _i32(entry + f_total) - left,
                        'sourceOwner': _i32(entry + f_side), 'sourceObjectId': (ids or {}).get(_u64(entry + f_source)),
                        'hitSpeedPercent': hit, 'speedPercent': speed, 'spawnSpeedPercent': spawn,
                        'stopsMovement': speed <= -100, 'stopsAttacks': hit <= -100})
        return out, _i32(comp + f['buff.invisible'])

    def unit_states(self, battle, engine_checks=False):
        """Per object, the states a player reads off the board that objects() does not give (charging, dashes, hidden /
        invisible, status effects with time left, attack cycle, ability phase, spell areas' time left). One dict per
        object in objects() order; 'kind' says which keys it has:

          unit (troops, buildings, towers): name, state, stateName, hidden, immune, invisible, airborne, buffs, attack, charge,
                pushed, pushRemainingMs, shield, shieldMax, spawns / spawnTimerMs, leavesOnDeath, ability
          area (spell areas, 3000000 ids): name, remainingMs, durationMs
          projectile (4000000 ids): name, cardId, sourceObjectId, x, y, z, dx, dy (movement in the last tick), aimX, aimY
                  (where it flies: a homing shot's aim follows its target), aimObjectId (the homing target, None for shots
                  that fly to a point), startX, startY (where it was launched; the Executioner's axe flies back there)
          timed (objects without hitpoints that go off after a fuse: Giant Skeleton / Balloon / Bomb Tower death bombs):
                  name, fuseMs
          other: nothing more

        hidden  = the engine's own check (character vtable +0xF8): underground (Miner / Goblin Drill travelling, Mighty
                  Miner's ability), a Tesla folded away: not targeted and not hit by area damage.
        immune  = the engine's damage-immunity check (Bandit / Boss Bandit dashing, ...).
        airborne = the engine's own air check (character vtable +0x108): flying units, a unit knocked into the air (Giant
                  hero's slap); False for a flyer pinned down (Vines).
        invisible = invisible buffs on the unit (Royal Ghost, Archer Queen's ability): not targeted, area damage still hits.
        attack  = {stage (damage-ramp stage 0/1/2 of Inferno Tower / Dragon, Mighty Miner), timelineMs, loadMs, hitCount,
                   hitSpeedMs, loadTimeMs, cycleMs (timeline mod hit speed while attacking: the hit lands when it wraps),
                   dashCooldownMs} or None.
        charge  = run-up charge progress as objects() reports it (>= 10000: charging; -1: the unit has none).
        z       = height above the ground (Mega Knight's jump up to 3000, a unit knocked up); jumpRemainingMs = Mega Knight's
                  jump time left; surfaceAt = (x, y) where a Miner / Goblin Drill / Mighty Miner travelling underground comes
                  up; invisibleAgainMs = Royal Ghost's time until it turns invisible again after attacking.
        attack also has dashHop (Golden Knight's hop in its dash chain: 1, 2, ...), specialCooldownMs (the Fisherman's
                  countdown before he throws the hook), returnShot = {elapsedMs, totalMs} while a shot that comes back is out
                  (the Executioner's axe: 1500 ms there and back; the Fisherman's hook).
        shield / shieldMax = shield hit points left / full (Dark Prince, Guards, Royal Recruits; 0 / 0 without one): damage
                  takes the shield first and what is left of a hit that breaks it does not reach the hit points.
        pushed / pushRemainingMs = being knocked back (Log, Fireball, Rocket, Snowball, Bowler, Mega Knight's landing, ...): the
                  movement component's push flag and push countdown; no effect is listed for a push.
        spawns / spawnTimerMs = for units that spawn on a timer (huts, Tombstone, Witch, the Phoenix egg hatching): what, and
                  the time to the next spawn. leavesOnDeath = what its data leaves when it dies (death spawn, death projectile).
        Units with a hero / champion ability also get ability = {controllerSlot, buttonState, remainingCooldownMs,
        configuredCooldownMs, remainingChargesRaw, maxCharges, uses, used, effectDelayMs, phase, phaseElapsedMs}:
          used / uses = whether / how often this unit's ability fired (uses = maxCharges - charges left: Boss Bandit's
          2 warps; a champion played again after dying starts at 0 again); effectDelayMs = time until the ability's effect starts;
          phase = 'casting' (state 10, or the effect delay running), 'active' (its effect is on: an ability buff, Monk's
          deflect, Mighty Miner underground or resurfacing, Golden Knight's dash chain) or None;
          phaseElapsedMs = time into an active buff-driven effect (from the buff's own clock), else None.
          Time since the use for every ability: AbilityClock."""
        f = self.f
        G = self.g
        hidden_slot = f['vslot.character_hidden']
        air_slot = f['vslot.character_air']
        immune = self._predicate(G.addr(bindings.of(G).fn('character_damage_immune')), immune=True)
        predicate = self._predicate
        ability_of = {}
        logic = self.logic(battle)
        f_controllers, f_champions = f['player.ability_controllers'], f['controller.champions']
        for owner in (0, 1):
            player = self.player(logic, owner)
            live = False  # any hero / champion unit on the board for this player (abilities() reads much more)
            for index in range(2):
                controller = _u64(player + f_controllers + 8 * index)
                vector = _u64(controller + f_champions) if controller else 0
                if vector and 0 < _i32(vector + 12) <= 64:
                    live = True
            if not live:
                continue
            for row in self.abilities(player):
                controller = _u64(player + f_controllers + 8 * (row['controllerSlot'] - 1))
                for unit in row['championObjectIds']:
                    charges, most = row['remainingChargesRaw'], row['maxCharges']
                    uses = most - charges if most and most > 0 and charges >= 0 else None
                    # the controller's used flag stays set after the champion dies; played again, its charges are full
                    # again (checked: Archer Queen, Boss Bandit), so the count decides when there is one
                    ability_of[unit] = {'controllerSlot': row['controllerSlot'], 'buttonState': row['buttonState'],
                                        'remainingCooldownMs': row['remainingCooldownMs'],
                                        'configuredCooldownMs': row['configuredCooldownMs'],
                                        'remainingChargesRaw': charges, 'maxCharges': most, 'uses': uses,
                                        'used': uses > 0 if uses is not None else _i32(controller + f['controller.used']) != 0,
                                        'effectDelayMs': max(0, _i32(controller + f['controller.effect_delay']))}
        f_id, f_data, f_state, f_count, f_comps = (f['entity.id'], f['entity.data'], f['entity.state'],
                                                   f['entity.component_count'], f['entity.components'])
        f_charge, f_pushed, f_push_left = f['movement.charge'], f['movement.pushed'], f['movement.push_remaining']
        f_timeline, f_stage, f_load, f_hits, f_dash = (f['attack.timeline'], f['attack.stage'], f['attack.load'],
                                                       f['attack.hit_count'], f['attack.dash_cooldown'])
        blocks = self._blocks
        if blocks is None:  # one read each for the fields read together (offsets of this build)
            blocks = self._blocks = (_Block([(0, 'Q'), (f_count, 'i'), (f_comps, 'Q'), (f_data, 'Q')]),
                                     _Block([(f_charge, 'i'), (f_pushed, 'i'), (f_push_left, 'i'), (f['movement.jump_remaining'], 'i'),
                                             (f['movement.surface_x'], 'i'), (f['movement.surface_y'], 'i')]),
                                     _Block([(f_timeline, 'i'), (f_stage, 'i'), (f_load, 'i'), (f_hits, 'i'), (f_dash, 'i'),
                                             (f['attack.dash_hop'], 'i'), (f['attack.return_shot'], 'Q'),
                                             (f['attack.return_elapsed'], 'i'), (f['attack.return_total'], 'i'),
                                             (f['attack.special_cooldown'], 'i')]),
                                     _Block([(f['health.shield'], 'i'), (f['health.shield_max'], 'i')]))
        head, motion, timing, guard = (b.read for b in blocks)
        names = self.STATE_NAMES
        out = []
        pointers = self.object_pointers(battle)
        ids = {p: _i32(p + f_id) & 0xFFFFFFFF for p in pointers if p}
        for e in pointers:
            if not e:
                continue
            oid = ids[e]
            vtable, count, comps, data = head(e)
            if count <= 0:
                comps = 0
            health = _u64(comps + 16) if count > 2 else 0
            if health:
                state = _i32(e + f_state)
                buffs, invisible = self.buffs(e, ids)
                attack = _u64(comps) if count > 0 else 0
                movement = _u64(comps + 8) if count > 1 else 0
                charge, pushed, push_left, jump_left, surface_x, surface_y = motion(movement) if movement else (None, 0, 0, 0, -1, -1)
                shield, shield_max = guard(health)
                if engine_checks or not data:  # the engine's own functions (the reference the field reads are checked against)
                    hidden, immune_now, airborne = (bool(predicate(_u64(vtable + hidden_slot))(e) & 1), bool(immune(e) & 1),
                                                    bool(predicate(_u64(vtable + air_slot))(e) & 1))
                else:
                    hidden, immune_now, airborne = self._checks(e, state, data)
                row = {'nativeObjectId': oid, 'kind': 'unit', 'name': '',
                       'state': state, 'stateName': names.get(state),
                       'hidden': hidden, 'immune': immune_now, 'invisible': invisible > 0, 'airborne': airborne,
                       'buffs': buffs, 'attack': None,
                       'charge': charge,
                       'pushed': pushed == 1,
                       'pushRemainingMs': max(0, push_left),
                       'shield': shield, 'shieldMax': shield_max,
                       'z': _i32(e + f['character.height']), 'jumpRemainingMs': max(0, jump_left),
                       'invisibleAgainMs': max(0, _i32(e + f['character.invisible_again'])),
                       'surfaceAt': (surface_x, surface_y) if state in (6, 7) and surface_x >= 0 else None}
                if data:
                    name, spawns, left, hit_speed, load_time = self._unit_data(data)
                    row['name'] = name
                    if 5000000 <= oid < 5000006 and 'King' in name:
                        # kind 12 while the King Tower sleeps, 13 once awake (70 ticks after a hit or a fallen
                        # princess tower; crx/tower_probe.py, 2026-10-12)
                        row['kingAwake'] = _i32(e + f['entity.kind']) != 12
                    elif name == 'DaggerDuchess':
                        # daggers left (8, -1 a throw, +1 per 0.9 s idle): the burst action's charge count. The tower's
                        # actions are a pointer array at entity +0x28 (count +0x30); the burst runner is slot 10, its
                        # first word the action (all three builds). The older read through +0x270 / +0x260 holds only
                        # in an engine's first battle: after a restart (every later replay of the converter / oracle)
                        # that field is something else (found 2026-10-12 by a whole-memory search on a second battle; the Duchess acts the same in every battle)
                        actions, count = _u64(e + f['entity.actions']), _i32(e + f['entity.action_count'])
                        near = lambda p: p and not p & 7 and abs(p - e) < 1 << 32  # noqa: E731
                        runner = _u64(actions + 8 * BURST_SLOT) if near(actions) and count > BURST_SLOT else 0
                        action = _u64(runner) if near(runner) else 0
                        daggers = _i32(action + f['burstaction.charges']) if near(action) else None
                        row['daggers'] = daggers if daggers is not None and 0 <= daggers <= 8 else None
                    if spawns is not None:
                        row['spawns'] = spawns
                        row['spawnTimerMs'] = _i32(e + f['entity.spawn_timer'])
                    row['leavesOnDeath'] = list(left)
                    if attack:
                        timeline, stage, load, hits, dash, hop, shot, shot_elapsed, shot_total, special = timing(attack)
                        row['attack'] = {'stage': stage, 'timelineMs': timeline, 'loadMs': load, 'hitCount': hits,
                                         'hitSpeedMs': hit_speed, 'loadTimeMs': load_time,
                                         'cycleMs': timeline % hit_speed if state == 2 and hit_speed > 0 else None,
                                         'dashCooldownMs': dash, 'dashHop': hop, 'specialCooldownMs': max(0, special),
                                         'returnShot': {'elapsedMs': shot_elapsed, 'totalMs': shot_total}
                                         if shot and shot_total > 0 else None}
                if oid in ability_of:
                    ability = row['ability'] = dict(ability_of[oid])
                    own = [b for b in buffs if b['name'] in self.ABILITY_BUFFS and b['sourceObjectId'] == oid]
                    active = bool(own) or state in (7, 16) or (ability['used'] and state == 4) \
                        or (ability['used'] and state == 3 and row['name'] == 'GoldenKnight')
                    ability['phase'] = 'casting' if state == 10 else 'active' if active \
                        else 'casting' if ability['effectDelayMs'] > 0 else None
                    ability['phaseElapsedMs'] = own[0]['elapsedMs'] if own and ability['phase'] == 'active' else None
            elif 3000000 <= oid < 4000000:
                row = {'nativeObjectId': oid, 'kind': 'area', 'name': self._name(data) if data else '',
                       'remainingMs': _i32(e + f['area.remaining']),
                       'durationMs': _i32(data + f['areadata.life']) if data else None}
            elif 4000000 <= oid < 5000000:
                x, y = _i32(e + f['entity.x']), _i32(e + f['entity.y'])
                row = {'nativeObjectId': oid, 'kind': 'projectile', 'name': self._name(data) if data else '',
                       'cardId': _i32(e + f['entity.card']), 'sourceObjectId': _i32(e + f['projectile.source_id']) & 0xFFFFFFFF,
                       'x': x, 'y': y, 'z': _i32(e + f['projectile.z']),
                       'dx': x - _i32(e + f['projectile.previous_x']), 'dy': y - _i32(e + f['projectile.previous_y']),
                       'aimX': _i32(e + f['projectile.aim_x']), 'aimY': _i32(e + f['projectile.aim_y']),
                       'aimObjectId': ids.get(_u64(e + f['projectile.aim_object'])),
                       'startX': _i32(e + f['projectile.start_x']), 'startY': _i32(e + f['projectile.start_y'])}
            elif oid >= 5000000 and comps and _u64(comps):
                row = {'nativeObjectId': oid, 'kind': 'timed', 'name': self._name(data) if data else '',
                       'fuseMs': _i32(e + f['entity.deploy_remaining'])}
            else:
                row = {'nativeObjectId': oid, 'kind': 'other'}
            out.append(row)
        return out

    def effect_table(self, tables):
        """Every effect (buff) the game data defines, from the loaded data tables (CrxEnv.tables): [{name, globalId,
        hitSpeedPercent, speedPercent, spawnSpeedPercent, stopsMovement, stopsAttacks}]. Older effects have ids 9000000+,
        newer ones a 32-bit id: use the name as the key."""
        f = self.f
        table = _u64(tables + f['tables.buffs'])
        rows, count = _u64(table + f['table.rows']), _i32(table + f['table.count'])
        out = []
        for i in range(max(0, min(count, 4096))):
            a = _u64(rows + 8 * i)
            out.append({'name': self.text(a + f['named.name']), 'globalId': _i32(a + f['data.id']) & 0xFFFFFFFF,
                        'hitSpeedPercent': _i32(a + f['buffdata.hit_speed']), 'speedPercent': _i32(a + f['buffdata.speed']),
                        'spawnSpeedPercent': _i32(a + f['buffdata.spawn_speed']),
                        'stopsMovement': _i32(a + f['buffdata.speed']) <= -100,
                        'stopsAttacks': _i32(a + f['buffdata.hit_speed']) <= -100})
        return out

    def frame(self, battle):
        return {'tick': _i32(battle + self.f['battle.tick']), 'state': {'players': self.players_state(battle)},
                'players': self.players_lean(battle), 'objects': self.objects(battle)}


class AbilityClock:
    """Time since each hero / champion unit last used its ability, from unit_states() rows seen over time (the engine keeps
    no such timer). Call update(tick, unit_states) every tick for exact values; with a coarser cadence a use is placed at the
    first tick it was seen, except when the ability has a cooldown (then from the cooldown: exact)."""

    def __init__(self):
        self.last = {}          # unit id -> (uses or used, use tick)

    def update(self, tick, states):
        for s in states:
            ability = s.get('ability')
            if not ability:
                continue
            count = ability['uses'] if ability['uses'] is not None else int(ability['used'])
            prev = self.last.get(s['nativeObjectId'])
            if prev is None or count != prev[0]:
                if count == 0:
                    at = None
                elif ability['configuredCooldownMs'] > 0 and ability['remainingCooldownMs'] > 0:
                    # the cooldown starts inside the use tick: configured - 50 at the use tick itself
                    at = tick - (ability['configuredCooldownMs'] - ability['remainingCooldownMs']) // 50 + 1
                else:
                    at = tick if prev is not None else None
                self.last[s['nativeObjectId']] = (count, at)
            at = self.last[s['nativeObjectId']][1]
            ability['sinceUseMs'] = 50 * (tick - at) if at is not None else None
        return states
