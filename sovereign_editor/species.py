"""Qualified species balance, learnsets, level evolution and machine compatibility.

Pure byte encoders for the pinned Sovereign Gold build; Project owns all writes.
Custom layouts differ from stock HGSS (docs/GAMEPLAY_AUTHORING_V1_IMPLEMENTATION.md):
personal records keep u16 abilities at 22/26; level-up moves are one flattened
table of 34 u32 rows; machine compatibility is an external 11-word bitfield row.
Unselected bytes, entries, methods and bits are preserved exactly.
"""
import struct

from .formats import require

PERSONAL = 'a/0/0/2'
LEVELUP = 'a/0/3/3'
EVOLUTION = 'a/0/3/4'
ADDONS = 'a/0/2/8'
MACHINE_MEMBER = 14
PERSONAL_SIZE, EVOLUTION_SIZE = 44, 56
ROW_ENTRIES = 34                       # MAX_LEVELUP_MOVES, including the terminator
ROW_BYTES = ROW_ENTRIES * 4
MACHINE_WORDS = 11                     # (NUM_MACHINE_MOVES 340 + 31) / 32
MACHINE_BYTES = MACHINE_WORDS * 4
END = 0x0000ffff
LEVEL_METHOD = 4                       # EVO_LEVEL: target when param <= level
EVOLUTION_SLOTS = 9                    # MAX_EVOS_PER_POKE
STATS = ('hp', 'attack', 'defense', 'speed', 'sp_attack', 'sp_defense')
GROWTH = {0: 'Medium Fast', 1: 'Erratic', 2: 'Fluctuating', 3: 'Medium Slow', 4: 'Fast', 5: 'Slow'}
TYPE_BANK, ABILITY_BANK, ITEM_BANK = 735, 720, 222
TYPES = range(0, 18)                   # Normal..Dark with Fairy at 9; ??? and Stellar excluded
ABILITIES = range(1, 124)              # stock Gen 4 abilities; expanded IDs are preserved, not offered
BASE_MACHINES = range(0, 100)          # TM001..TM092, HM01..HM08: items 328..427 -> index item-328
FIRST_MACHINE_ITEM = 328
MACHINE_TABLE = 0x023dea68             # sMachineMoves in overlay 129 (hooked ItemToMachineMove)
EDIT_FIELDS = {'stats', 'types', 'abilities', 'growth', 'learnset', 'evolutions', 'machines'}


def members(project, state, species):
    """Current bytes of the four records that describe one base species."""
    from .gameplay import current
    personal = current(project, state, PERSONAL, species)
    table = current(project, state, LEVELUP, 0)
    evolution = current(project, state, EVOLUTION, species)
    machines = current(project, state, ADDONS, MACHINE_MEMBER)
    return {'personal': personal, 'learnset': table[species * ROW_BYTES:(species + 1) * ROW_BYTES],
            'evolution': evolution, 'machines': machines[species * MACHINE_BYTES:(species + 1) * MACHINE_BYTES]}


def fingerprint(parts):
    from .formats import digest
    return digest(b''.join(parts[k] for k in ('personal', 'learnset', 'evolution', 'machines')))


def decode_personal(raw):
    require(len(raw) == PERSONAL_SIZE, 'Unsupported personal record size', 'UNSUPPORTED_SPECIES')
    return {'stats': dict(zip(STATS, raw[0:6])), 'types': [raw[6], raw[7]], 'growth': raw[19],
            'abilities': list(struct.unpack_from('<H', raw, 22) + struct.unpack_from('<H', raw, 26))}


def decode_learnset(raw):
    require(len(raw) == ROW_BYTES, 'Unsupported learnset row', 'UNSUPPORTED_SPECIES')
    words = struct.unpack(f'<{ROW_ENTRIES}I', raw)
    require(END in words, 'Learnset row has no terminator; this layout is read-only', 'UNSUPPORTED_SPECIES')
    used = words[:words.index(END)]
    return [{'level': w >> 16, 'move': w & 0xffff} for w in used]


def decode_evolutions(raw):
    require(len(raw) == EVOLUTION_SIZE and raw[54:] == b'\0\0',
            'Only the 56-byte nine-entry evolution layout is supported', 'UNSUPPORTED_SPECIES')
    rows = []
    for slot in range(EVOLUTION_SLOTS):
        method, param, target = struct.unpack_from('<3H', raw, slot * 6)
        rows.append({'slot': slot, 'method': method, 'param': param, 'target': target & 0x7ff,
                     'form': target >> 11, 'editable': method in (0, LEVEL_METHOD) and target >> 11 == 0})
    return rows


def decode_machines(raw):
    require(len(raw) == MACHINE_BYTES, 'Unsupported machine compatibility row', 'UNSUPPORTED_SPECIES')
    words = struct.unpack(f'<{MACHINE_WORDS}I', raw)
    return [i for i in range(MACHINE_WORDS * 32) if words[i // 32] >> (i % 32) & 1]


def machine_moves(project):
    """Qualified item -> machine index -> move mapping read from the hooked table."""
    cache = getattr(project, '_machine_moves', None)
    if cache is None:
        from .character_runtime import overlay
        ext = overlay(project.blob, 129)
        at = MACHINE_TABLE - ext['address']
        moves = struct.unpack_from('<100H', ext['data'], at)
        from .gameplay import name
        cache = []
        for index in BASE_MACHINES:
            item = FIRST_MACHINE_ITEM + index
            label = name(project, ITEM_BANK, item) or ''
            expected = f'TM{index + 1:03d}' if index < 92 else f'HM{index - 91:02d}'
            require(label.upper() == expected, f'Machine item {item} is not {expected}', 'UNSUPPORTED_RUNTIME')
            cache.append({'index': index, 'item': item, 'name': expected, 'move': moves[index]})
        project._machine_moves = cache
    return cache


def label(project, kind, value):
    from .gameplay import name
    if kind == 'types':
        return name(project, TYPE_BANK, value)
    if kind == 'abilities':
        return name(project, ABILITY_BANK, value)
    return GROWTH.get(value)


def catalog_rows(project, kind):
    if kind == 'types':
        return [{'id': i, 'name': label(project, 'types', i), 'supported': True, 'reason': ''} for i in TYPES]
    if kind == 'abilities':
        return [{'id': i, 'name': label(project, 'abilities', i) or f'Ability {i}', 'supported': True, 'reason': ''}
                for i in ABILITIES]
    if kind == 'growth':
        return [{'id': i, 'name': n, 'supported': True, 'reason': ''} for i, n in GROWTH.items()]
    return [{'id': m['index'], 'name': m['name'], 'item': m['item'], 'move': m['move'],
             'supported': True, 'reason': ''} for m in machine_moves(project)]


def view(project, state, species):
    from .gameplay import entry, valid_id
    valid_id(project, 'species', species)
    parts = members(project, state, species)
    result = {'species': species, 'name': entry(project, 'species', species)['name'],
              'before_sha256': fingerprint(parts), 'editable': {}, 'reasons': {}}
    for key, decode in (('personal', decode_personal), ('learnset', decode_learnset),
                        ('evolution', decode_evolutions), ('machines', decode_machines)):
        try:
            result[key] = decode(parts[key]); result['editable'][key] = True
        except Exception as exc:  # EditorError: report the read-only reason
            result['editable'][key] = False; result['reasons'][key] = str(exc)
    if 'machines' in result:
        result['machines'] = {'base_compatible': [i for i in result['machines'] if i in BASE_MACHINES],
                              'other_bits_set': len([i for i in result['machines'] if i not in BASE_MACHINES])}
    return result


def _move(project, move):
    from .gameplay import valid_id
    require(type(move) is int and move > 0, 'Choose a move', 'INVALID_INPUT')
    valid_id(project, 'moves', move)


def encode(project, parts, species, changes):
    """Return new record parts plus advisories. Refuses rather than normalizes."""
    require(isinstance(changes, dict) and changes and set(changes) <= EDIT_FIELDS,
            'Species changes use stats, types, abilities, growth, learnset, evolutions or machines', 'INVALID_INPUT')
    out = dict(parts); advisories = []
    if {'stats', 'types', 'abilities', 'growth'} & set(changes):
        raw = bytearray(parts['personal']); decode_personal(raw)
        if 'stats' in changes:
            stats = changes['stats']
            require(isinstance(stats, dict) and stats and set(stats) <= set(STATS), 'Unknown base stat', 'INVALID_INPUT')
            for key, value in stats.items():
                require(type(value) is int and 1 <= value <= 255, 'Base stats must be 1..255', 'INVALID_INPUT')
                raw[STATS.index(key)] = value
        if 'types' in changes:
            types = changes['types']
            require(isinstance(types, list) and len(types) == 2 and all(type(t) is int and t in TYPES for t in types),
                    'Choose two supported types (repeat one for a single type)', 'INVALID_INPUT')
            raw[6:8] = bytes(types)
        if 'abilities' in changes:
            abilities = changes['abilities']
            require(isinstance(abilities, list) and len(abilities) == 2 and all(type(a) is int for a in abilities)
                    and abilities[0] in ABILITIES and (abilities[1] == 0 or abilities[1] in ABILITIES),
                    'Choose a supported first ability and an optional second ability', 'INVALID_INPUT')
            struct.pack_into('<H', raw, 22, abilities[0]); struct.pack_into('<H', raw, 26, abilities[1])
            advisories.append('Ability slots choose an existing ability; they do not add or verify its battle effect.')
        if 'growth' in changes:
            require(type(changes['growth']) is int and changes['growth'] in GROWTH, 'Choose a growth group', 'INVALID_INPUT')
            raw[19] = changes['growth']
        out['personal'] = bytes(raw)
    if 'learnset' in changes:
        edit = changes['learnset']
        require(isinstance(edit, dict) and edit and set(edit) <= {'add', 'remove'}, 'Learnset edits add or remove entries', 'INVALID_INPUT')
        rows = decode_learnset(parts['learnset'])
        for r in edit.get('remove', []):
            require(isinstance(r, dict) and set(r) == {'level', 'move'} and r in rows,
                    'A removed learnset entry must exist exactly', 'BEFORE_VALUE_MISMATCH')
            rows.remove(r)
        for r in edit.get('add', []):
            require(isinstance(r, dict) and set(r) == {'level', 'move'} and type(r['level']) is int and 0 <= r['level'] <= 100,
                    'Learnset entries need level 0..100 (0 = on evolution) and a move', 'INVALID_INPUT')
            _move(project, r['move'])
            require(r not in rows, 'This learnset entry already exists', 'DUPLICATE_EDIT')
            at = max([i + 1 for i, old in enumerate(rows) if old['level'] <= r['level']], default=0)
            rows.insert(at, dict(r))
            if any(o['move'] == r['move'] for o in rows if o is not rows[at]):
                advisories.append(f"Move {r['move']} appears at more than one level; a known move is skipped when relearned.")
        require(len(rows) < ROW_ENTRIES, f'Learnset capacity is {ROW_ENTRIES - 1} moves', 'RESOURCE_CAPACITY')
        words = [r['level'] << 16 | r['move'] for r in rows]
        out['learnset'] = struct.pack(f'<{ROW_ENTRIES}I', *(words + [END] * (ROW_ENTRIES - len(words))))
        # Everything after the terminator stays the stock END padding.
        require(set(struct.unpack(f'<{ROW_ENTRIES}I', parts['learnset'])[len(decode_learnset(parts['learnset'])):]) == {END},
                'Learnset padding differs from the qualified layout', 'UNSUPPORTED_SPECIES')
    if 'evolutions' in changes:
        edits = changes['evolutions']
        raw = bytearray(parts['evolution']); rows = decode_evolutions(raw); seen = set()
        require(isinstance(edits, list) and 1 <= len(edits) <= EVOLUTION_SLOTS, 'Edit 1..9 evolution slots', 'INVALID_INPUT')
        for e in edits:
            require(isinstance(e, dict) and type(e.get('slot')) is int and 0 <= e['slot'] < EVOLUTION_SLOTS
                    and e['slot'] not in seen, 'Choose each evolution slot once', 'INVALID_INPUT')
            seen.add(e['slot']); old = rows[e['slot']]
            require(old['editable'], 'Only empty or ordinary level-evolution slots are editable; other methods are preserved',
                    'UNSUPPORTED_SPECIES')
            if e.get('clear'):
                require(set(e) == {'slot', 'clear'} and e['clear'] is True, 'Invalid evolution clear', 'INVALID_INPUT')
                struct.pack_into('<3H', raw, e['slot'] * 6, 0, 0, 0)
                continue
            require(set(e) == {'slot', 'level', 'target'} and type(e['level']) is int and 1 <= e['level'] <= 100,
                    'Level evolution needs slot, level 1..100 and target', 'INVALID_INPUT')
            from .gameplay import valid_id
            valid_id(project, 'species', e['target'])
            require(e['target'] != species, 'A species cannot evolve into itself', 'INVALID_INPUT')
            struct.pack_into('<3H', raw, e['slot'] * 6, LEVEL_METHOD, e['level'], e['target'])
        levels = [r for r in decode_evolutions(raw) if r['method'] == LEVEL_METHOD]
        if len(levels) > 1:
            advisories.append('Several level evolutions: the engine keeps the last qualifying slot.')
        out['evolution'] = bytes(raw)
    if 'machines' in changes:
        edits = changes['machines']
        require(isinstance(edits, list) and edits, 'Choose machine compatibility edits', 'INVALID_INPUT')
        words = list(struct.unpack(f'<{MACHINE_WORDS}I', parts['machines'])); seen = set()
        machine_moves(project)
        for e in edits:
            require(isinstance(e, dict) and set(e) == {'machine', 'compatible'} and type(e['machine']) is int
                    and e['machine'] in BASE_MACHINES and type(e['compatible']) is bool and e['machine'] not in seen,
                    'Choose each base TM/HM (0..99) once with true or false', 'INVALID_INPUT')
            seen.add(e['machine']); i = e['machine']
            words[i // 32] = words[i // 32] | (1 << i % 32) if e['compatible'] else words[i // 32] & ~(1 << i % 32)
        out['machines'] = struct.pack(f'<{MACHINE_WORDS}I', *words)
    return out, advisories


def impact(project, state, species_ids):
    """Global users of changed species records: trainers, encounter files, evolution sources."""
    from .gameplay import TRAINERS, PARTIES, WILD, archive, current, decode_team, decode_wild, TIMES, METHODS
    from . import world
    wanted = set(species_ids)
    count = struct.unpack_from('<H', archive(project, TRAINERS), 24)[0]
    trainers = []
    for tid in range(1, count):
        try:
            rows = decode_team(current(project, state, TRAINERS, tid), current(project, state, PARTIES, tid))
        except Exception:
            continue
        hit = sorted({r['species'] for r in rows} & wanted)
        if hit:
            trainers.append({'id': tid, 'species': hit})
    story = state.get('story', {}).get('trainer', {})
    authored = [{'key': k, 'trainer_id': t['trainer_id'], 'species': sorted({m['species'] for m in t['party']} & wanted)}
                for k, t in story.items() if {m['species'] for m in t['party']} & wanted]
    wild_users = {}
    for h in range(project.header_count()):
        head = project.header(h)
        wild_users.setdefault(head['wild_pokemon'], []).append(h)
    encounters = []
    wild_count = struct.unpack_from('<H', archive(project, WILD), 24)[0]
    created = sorted(m for a, m in getattr(project, '_world_members', {}) if a == WILD)
    for member in list(range(wild_count)) + created:
        raw = current(project, state, WILD, member)
        if len(raw) != 196:
            continue
        view = decode_wild(raw)
        ids = set(sum((view['grass'][t] for t in TIMES), []))
        for m in METHODS:
            ids.update(s['species'] for s in view[m]['slots'])
        hit = sorted(ids & wanted)
        if hit:
            encounters.append({'member': member, 'headers': wild_users.get(member, []), 'species': hit})
    sources = []
    evo_count = struct.unpack_from('<H', archive(project, EVOLUTION), 24)[0]
    for s in range(1, min(evo_count, 494)):
        raw = current(project, state, EVOLUTION, s)
        if len(raw) != EVOLUTION_SIZE:
            continue
        for slot in range(EVOLUTION_SLOTS):
            method, param, target = struct.unpack_from('<3H', raw, slot * 6)
            if method and target & 0x7ff in wanted:
                sources.append({'species': s, 'slot': slot, 'method': method, 'param': param, 'target': target & 0x7ff})
    return {'trainers': trainers, 'authored_trainers': authored, 'encounter_files': encounters,
            'evolution_sources': sources,
            'notes': ['Species records are global: every listed trainer, encounter and evolution uses the new values.',
                      'Stored party/box Pokémon keep their saved moves, ability number and experience; use fresh Pokémon to inspect edits.',
                      'Growth changes reinterpret stored experience; existing saved levels are not converted.']}


# ---- v3: typed non-level evolution methods and form targets (gameplay v3) ----------
# Method numbers: sovereign-gold include/pokemon.h EvoMethod; consumers are BOTH
# GetMonEvolutionInternal copies (overlay 133 field, 134 battle), byte-matched and
# CPU-qualified by tools/assets_gameplay_qualification.py. Every other method (trade,
# location, beauty, nature, gender, party, spin, ...) is preserved and read-only.
EVOLUTION_METHODS = {
    'friendship': (1, 'none'), 'friendship_day': (2, 'none'), 'friendship_night': (3, 'none'),
    'level': (4, 'level'), 'item': (7, 'use_item'), 'held_item_day': (18, 'held_item'),
    'held_item_night': (19, 'held_item'), 'known_move': (20, 'move'), 'level_day': (27, 'level'),
    'level_night': (28, 'level'), 'known_move_type': (31, 'type')}
METHOD_NAMES = {number: name for name, (number, _) in EVOLUTION_METHODS.items()}
FRIENDSHIP_THRESHOLD = 160          # config.h; CPU-checked at 159/160


def evolution_items(project):
    """Items the runtime actually accepts for use-on-Pokémon evolution: every item the
    pinned ROM already uses as an EVO_STONE parameter (the bag routes these to evolution)."""
    cached = getattr(project, '_evolution_items', None)
    if cached is None:
        from .formats import file_span, member_span
        raw = file_span(project.blob, EVOLUTION)[1]
        items = set()
        for member in range(struct.unpack_from('<H', raw, 24)[0]):
            record = member_span(raw, member)[1]
            for slot in range(min(len(record) // 6, EVOLUTION_SLOTS)):
                method, param, _ = struct.unpack_from('<3H', record, slot * 6)
                if method == 7 and param:
                    items.add(param)
        cached = project._evolution_items = sorted(items)
    return cached


def decode_evolutions_v3(raw):
    require(len(raw) == EVOLUTION_SIZE and raw[54:] == b'\0\0',
            'Only the 56-byte nine-entry evolution layout is supported', 'UNSUPPORTED_SPECIES')
    rows = []
    for slot in range(EVOLUTION_SLOTS):
        method, param, target = struct.unpack_from('<3H', raw, slot * 6)
        rows.append({'slot': slot, 'method': METHOD_NAMES.get(method, f'method {method}') if method else None,
                     'method_id': method, 'param': param, 'target': target & 0x7ff, 'form': target >> 11,
                     'editable': method == 0 or method in METHOD_NAMES})
    return rows


def _evolution_param(project, kind, value):
    from .gameplay import valid_id
    if kind == 'none':
        require(value in (None, 0), 'This method takes no parameter', 'INVALID_INPUT')
        return 0
    if kind == 'level':
        require(type(value) is int and 1 <= value <= 100, 'Level must be 1..100', 'INVALID_INPUT')
    elif kind == 'use_item':
        require(type(value) is int and value in evolution_items(project),
                'Choose an evolution item this build routes to evolution (see catalog evolution_items)', 'INVALID_INPUT')
    elif kind == 'held_item':
        require(type(value) is int and value > 0, 'Choose a held item', 'INVALID_INPUT')
        valid_id(project, 'items', value)
    elif kind == 'move':
        require(type(value) is int and value > 0, 'Choose a move', 'INVALID_INPUT')
        valid_id(project, 'moves', value)
    elif kind == 'type':
        require(type(value) is int and value in TYPES, 'Choose a supported type', 'INVALID_INPUT')
    return value


def encode_evolutions_v3(project, raw, index, edits):
    """Typed evolution slot edits for personal index ``index``. Returns (bytes, advisories)."""
    from . import species_forms as sf
    raw = bytearray(raw); rows = decode_evolutions_v3(raw); seen = set(); advisories = []
    require(isinstance(edits, list) and 1 <= len(edits) <= EVOLUTION_SLOTS, 'Edit 1..9 evolution slots', 'INVALID_INPUT')
    for e in edits:
        require(isinstance(e, dict) and type(e.get('slot')) is int and 0 <= e['slot'] < EVOLUTION_SLOTS
                and e['slot'] not in seen, 'Choose each evolution slot once', 'INVALID_INPUT')
        seen.add(e['slot']); old = rows[e['slot']]
        require(old['editable'], f"Slot {e['slot']} uses {old['method']}, which is preserved read-only", 'UNSUPPORTED_SPECIES')
        if e.get('clear'):
            require(set(e) == {'slot', 'clear'} and e['clear'] is True, 'Invalid evolution clear', 'INVALID_INPUT')
            struct.pack_into('<3H', raw, e['slot'] * 6, 0, 0, 0)
            continue
        if set(e) == {'slot', 'level', 'target'} and type(e['target']) is int:
            # The v2 request shape (ordinary level evolution) keeps working unchanged.
            e = {'slot': e['slot'], 'method': 'level', 'param': e['level'], 'target': {'species': e['target']}}
        require(set(e) <= {'slot', 'method', 'param', 'target'} and e.get('method') in EVOLUTION_METHODS,
                f'Evolution methods: {", ".join(EVOLUTION_METHODS)}', 'INVALID_INPUT')
        number, kind = EVOLUTION_METHODS[e['method']]
        param = _evolution_param(project, kind, e.get('param'))
        target = e.get('target')
        require(isinstance(target, dict) and set(target) <= {'species', 'form'} and 'species' in target,
                'Target is {species, form}', 'INVALID_INPUT')
        form = target.get('form', 0)
        row = sf.require_supported(project, target['species'], form, 'Evolution target')
        require(row['personal_index'] != index, 'A species cannot evolve into itself', 'INVALID_INPUT')
        struct.pack_into('<3H', raw, e['slot'] * 6, number, param, sf.pack(target['species'], form))
        if kind == 'type':
            advisories.append(f'Known-move-type evolution also requires friendship >= {FRIENDSHIP_THRESHOLD} (engine rule).')
        if e['method'].endswith(('_day', '_night')):
            advisories.append('Day/night uses the RTC IsNighttime split of this build.')
    if len([r for r in decode_evolutions_v3(bytes(raw)) if r['method_id']]) > 1:
        advisories.append('Level-up checks run slot by slot; the first qualifying slot wins (both runtime copies).')
    return bytes(raw), advisories


def relearnable(rows, level, known):
    """Moves the pinned relearner offers (ARM9 0x0209176C, patched for 34 u32 rows):
    every entry with level <= current level (level-0 evolution moves included), in
    table order, skipping known moves and duplicates."""
    offered = []
    for r in rows:
        if r['level'] > level or r['move'] in known or r['move'] in offered:
            continue
        offered.append(r['move'])
    return offered
