"""Gameplay records (DATA-01..03): move records, item prices and use parameters, TM mappings
and the qualified expanded move/item/ability catalogs. Project owns writes.

Layouts (pinned engine build; tests/evidence under editor-completion-v1):
* a/0/1/1 move record, 16 bytes (engine armips movemacros.s): effect u16, category u8
  (0 physical, 1 special, 2 status), power, type, accuracy, PP, effect chance, target u16,
  priority s8, flags (0x20 FLAG_UNUSED_MOVE: unimplemented/unusable, and the build's
  BLOCK_LEARNING_UNIMPLEMENTED_MOVES zeroes such moves in trainer parties), appeal,
  contest type, terminator 0. 929 records, move IDs 0..928.
* a/0/1/7 item record, 36 bytes (pret ItemData; engine GetItemIndex(ITEM_GET_DATA) is the
  item ID): price u16; party-use block from byte 14 with flags (byte 19 bit 0 PP restore,
  bit 2 HP restore, bits 3..7 + byte 20 bit 0 EV gains, byte 20 bits 1..3 friendship)
  and parameters (EV gains 21..26 s8, HP restore 27, PP restore 28, friendship 29..31 s8).
  A parameter is editable only when its family flag is already set (no new use effect).
* TM table sMachineMoves (overlay 129 0x023DEA68, hooked ItemToMachineMove): 100 u16,
  TM001..TM092 and HM01..HM08. Compatibility bits are per machine index, so remapping a TM
  gives every species compatible with that index the new move (reported as impact).
  HMs stay stock: field authority is keyed to the move itself (field_moves.py).

* Shops (SERVICE-01, field_services.py): named inventories of existing priced, sellable
  items, sold through the native special mart; clerks reference them by name.

Existing records only: no new move, item, effect or ability code.
"""
import copy
import json
import re
import struct
from pathlib import Path

from . import authoring
from .formats import EditorError, baseline_digest, digest, require

SCHEMA = 'sovereign-game-data-v1'
SCHEMAS = (SCHEMA,)
MOVES, ITEMS = 'a/0/1/1', 'a/0/1/7'
MOVE_SIZE, ITEM_SIZE = 16, 36
MOVE_BANK, ITEM_BANK, ABILITY_BANK, TYPE_BANK = 750, 222, 720, 735
UNUSED_MOVE = 0x20
CATEGORIES = ('physical', 'special', 'status')
TYPES = range(0, 18)                  # Normal..Dark with Fairy at 9; ??? (18) and Stellar (19) excluded
TM_COUNT = 92
STOCK_ITEMS = range(1, 537)
MOVE_FIELDS = {'power': (3, 'B', 0, 255), 'accuracy': (5, 'B', 0, 100), 'pp': (6, 'B', 1, 40),
               'type': (4, 'B', 0, 17), 'category': (2, 'B', 0, 2), 'priority': (10, 'b', -7, 7),
               'effect': (0, 'H', 0, 0xFFFF), 'effect_chance': (7, 'B', 0, 100)}
EV_FLAGS = ((19, 3), (19, 4), (19, 5), (19, 6), (19, 7), (20, 0))
FRIENDSHIP_FLAGS = ((20, 1), (20, 2), (20, 3))
ITEM_FAMILIES = {'price': 'Mart price (all marts and the sell price)',
                 'hp_restore': 'HP restore amount (party menu and battle bag)',
                 'pp_restore': 'PP restore amount (party menu)',
                 'ev_gain': 'EV change per stat (vitamins and EV berries)',
                 'friendship': 'Friendship change by friendship band'}
_CATALOG = None


def engine_catalog():
    global _CATALOG
    if _CATALOG is None:
        _CATALOG = json.loads((Path(__file__).parent / 'assets/engine-catalog.json').read_text())
    return _CATALOG


def _members(project, state, path):
    from .gameplay import current
    return lambda i: current(project, state, path, i)


def _name(project, bank, index):
    from .gameplay import name
    return name(project, bank, index)


def _count(project, path):
    from .gameplay import archive
    return struct.unpack_from('<H', archive(project, path), 24)[0]


# ---- catalogs ----------------------------------------------------------------------------------

def move_entry(project, state, index):
    count = _count(project, MOVES)
    label = _name(project, MOVE_BANK, index) if index < count else ''
    raw = _members(project, state, MOVES)(index) if 0 < index < count else b''
    reason = ('No such move record' if not raw else 'Unnamed move' if not label else
              'Placeholder record (no move)' if re.fullmatch(r'MOVE_\d+', label) else
              'Unimplemented or unusable in this build (FLAG_UNUSED_MOVE)' if raw[11] & UNUSED_MOVE else
              'No PP (never selectable)' if not raw[6] else '')
    return {'id': index, 'name': label or f'Move {index}', 'expanded': index > 467, 'supported': not reason,
            'reason': reason}


def item_entry(project, state, index):
    count = _count(project, ITEMS)
    label = _name(project, ITEM_BANK, index) if index < count else ''
    raw = _members(project, state, ITEMS)(index) if 0 < index < count else b''
    reason = ('No such item record' if not raw else 'Unnamed item' if not label else
              'Placeholder record (no item)' if label.strip('?') == '' else '')
    hold = raw[2] if raw else 0
    held_ok = bool(hold) and engine_catalog()['hold_effects'].get(str(hold), {}).get('qualified', False)
    return {'id': index, 'name': label or f'Item {index}', 'expanded': index > 536, 'supported': not reason,
            'reason': reason, 'hold_effect': hold, 'holdable': held_ok and not reason,
            'hold_reason': '' if held_ok else ('No held effect' if not hold else
                                              engine_catalog()['hold_effects'].get(str(hold), {}).get('reason', 'Unknown hold effect'))}


def ability_entry(project, index):
    row = engine_catalog()['abilities'].get(str(index))
    label = _name(project, ABILITY_BANK, index)
    if row is None or not label:
        return {'id': index, 'name': label or f'Ability {index}', 'supported': False, 'reason': 'Not an engine ability'}
    return {'id': index, 'name': label, 'expanded': not row['stock'], 'supported': row['qualified'],
            'reason': '' if row['qualified'] else row['reason'], 'evidence': row['reason']}


def catalog(project, kind, search='', offset=0, limit=40, state=None):
    require(kind in ('moves', 'items', 'abilities', 'effects', 'machines', 'shops', 'spawns') and isinstance(search, str)
            and type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= 600,
            'Invalid catalog query', 'INVALID_INPUT')
    state = project.composed() if state is None else state
    if kind == 'moves':
        rows = [move_entry(project, state, i) for i in range(1, _count(project, MOVES))]
    elif kind == 'items':
        rows = [item_entry(project, state, i) for i in range(1, _count(project, ITEMS))]
    elif kind == 'abilities':
        rows = [ability_entry(project, i) for i in range(1, len(engine_catalog()['abilities']) + 1)]
    elif kind == 'effects':
        rows = effects(project, state)
    elif kind == 'shops':
        rows = shop_rows(project, state)
    elif kind == 'spawns':
        from . import field_services as fs
        rows = [r | {'supported': r['blackout'], 'reason': '' if r['blackout'] else 'Not a blackout spawn'}
                for r in fs.spawns(project.blob)]
    else:
        rows = machines(project, state)
    rows = [r for r in rows if search.lower() in r['name'].lower() or search == str(r['id'])]
    return {'kind': kind, 'total': len(rows), 'supported': sum(r.get('supported', True) for r in rows),
            'offset': offset, 'entries': rows[offset:offset + limit]}


def effects(project, state):
    """Battle effects already used by an implemented stock move (qualified selection)."""
    seen = {}
    for i in range(1, 468):
        entry = move_entry(project, state, i)
        if entry['supported']:
            effect = struct.unpack_from('<H', _members(project, state, MOVES)(i), 0)[0]
            seen.setdefault(effect, entry['name'])
    return [{'id': e, 'name': f'Effect {e} (as {label})', 'supported': True, 'reason': ''} for e, label in sorted(seen.items())]


def machines(project, state):
    from . import species as sp
    current = state.get('machine_moves', {})
    rows = []
    for row in sp.machine_moves(project):
        move = current.get(row['index'], row['move'])
        rows.append({'id': row['index'], 'name': f"{row['name']} · {_name(project, MOVE_BANK, move)}", 'item': row['item'],
                     'move': move, 'stock_move': row['move'], 'supported': row['index'] < TM_COUNT,
                     'reason': '' if row['index'] < TM_COUNT else 'HM mapping stays stock (field authority)'})
    return rows


def shop_rows(project, state):
    from . import field_services as fs
    index = fs.shop_index(state)
    return [{'id': index[name], 'name': name, 'items': list(items), 'users': fs.users(state, name),
             'item_names': [_name(project, ITEM_BANK, i) for i in items], 'notes': fs.shop_notes(project, state, items),
             'supported': True, 'reason': ''} for name, items in sorted(fs.shops(state).items())]


# ---- decoding ----------------------------------------------------------------------------------

def decode_move(raw):
    require(len(raw) == MOVE_SIZE, 'Unsupported move record size', 'UNSUPPORTED_RECORD')
    return {k: struct.unpack_from('<' + f, raw, at)[0] for k, (at, f, _, _) in MOVE_FIELDS.items()} | \
        {'target': struct.unpack_from('<H', raw, 8)[0], 'flags': raw[11], 'unused': bool(raw[11] & UNUSED_MOVE)}


def _bit(raw, at, bit): return bool(raw[at] >> bit & 1)


def decode_item(raw):
    require(len(raw) == ITEM_SIZE, 'Unsupported item record size', 'UNSUPPORTED_RECORD')
    view = {'price': struct.unpack_from('<H', raw, 0)[0], 'hold_effect': raw[2], 'field_use': raw[10],
            'battle_use': raw[11], 'party_use': raw[12]}
    if _bit(raw, 19, 2):
        view['hp_restore'] = raw[27]
    if _bit(raw, 19, 0):
        view['pp_restore'] = raw[28]
    if any(_bit(raw, a, b) for a, b in EV_FLAGS):
        view['ev_gain'] = [struct.unpack_from('<b', raw, 21 + i)[0] for i in range(6)]
    if any(_bit(raw, a, b) for a, b in FRIENDSHIP_FLAGS):
        view['friendship'] = [struct.unpack_from('<b', raw, 29 + i)[0] for i in range(3)]
    return view


# ---- planning ----------------------------------------------------------------------------------

def _int(v, lo, hi, what):
    require(type(v) is int and not isinstance(v, bool) and lo <= v <= hi, f'{what} must be {lo}..{hi}', 'INVALID_INPUT')


def encode_move(project, state, raw, changes):
    require(isinstance(changes, dict) and changes and set(changes) <= set(MOVE_FIELDS),
            f"Move changes are {', '.join(MOVE_FIELDS)}", 'INVALID_INPUT')
    out = bytearray(raw)
    for key, value in changes.items():
        at, fmt_, lo, hi = MOVE_FIELDS[key]
        _int(value, lo, hi, key.replace('_', ' ').capitalize())
        if key == 'effect':
            require(value in {e['id'] for e in effects(project, state)},
                    'Choose a battle effect already used by an implemented stock move', 'UNSUPPORTED_EFFECT')
        struct.pack_into('<' + fmt_, out, at, value)
    view = decode_move(bytes(out))
    require(view['category'] != 2 or view['power'] == 0, 'Status moves have no power', 'INVALID_INPUT')
    return bytes(out)


def encode_item(raw, changes):
    require(isinstance(changes, dict) and changes and set(changes) <= set(ITEM_FAMILIES),
            f"Item changes are {', '.join(ITEM_FAMILIES)}", 'INVALID_INPUT')
    out = bytearray(raw)
    view = decode_item(raw)
    for key, value in changes.items():
        require(key == 'price' or key in view,
                f"This item has no {ITEM_FAMILIES[key].split(' (')[0].lower()} use; new use effects are unsupported",
                'UNSUPPORTED_ITEM_USE')
        if key == 'price':
            _int(value, 0, 65535, 'Price'); struct.pack_into('<H', out, 0, value)
        elif key == 'hp_restore':
            _int(value, 1, 255, 'HP restore'); out[27] = value
        elif key == 'pp_restore':
            _int(value, 1, 255, 'PP restore'); out[28] = value
        else:
            size, at = (6, 21) if key == 'ev_gain' else (3, 29)
            require(isinstance(value, list) and len(value) == size, f'{key} needs {size} values', 'INVALID_INPUT')
            for i, v in enumerate(value):
                _int(v, -100, 100, key.replace('_', ' ')); struct.pack_into('<b', out, at + i, v)
    return bytes(out)


def impact(project, state, kind, ident, before=None, after=None):
    """Shared users of a record, so the author sees every consumer of a global change."""
    from . import species as sp, story_authoring as sa
    from .gameplay import current
    result = {'global': True}
    if kind == 'move':
        learn, tm = [], [m['name'] for m in machines(project, state) if m['move'] == ident]
        raw = current(project, state, sp.LEVELUP, 0)
        for s in range(1, len(raw) // sp.ROW_BYTES):
            row = raw[s * sp.ROW_BYTES:(s + 1) * sp.ROW_BYTES]
            words = struct.unpack(f'<{sp.ROW_ENTRIES}I', row)
            if any(w & 0xFFFF == ident and w != sp.END for w in words):
                learn.append(s)
        library = [k for k, t in sa.catalog(state, 'trainer').items()
                   if any((m.get('moves') or []) and ident in m['moves'] for m in t['party'])]
        tutors = [k for k, s in sa.catalog(state, 'sequence').items()
                  if any(n.get('op') == 'tutor' and n.get('move') == ident for n in s.get('nodes', []))]
        result.update(level_up_species=len(learn), level_up_examples=learn[:12], machines=tm,
                      library_trainers=library, tutors=tutors,
                      note='Every Pokémon, trainer and tutor using this move sees the change.')
    elif kind == 'item':
        library = [k for k, t in sa.catalog(state, 'trainer').items() if any(m.get('held_item') == ident for m in t['party'])]
        gifts = [k for k, s in sa.catalog(state, 'sequence').items()
                 if any(n.get('op') in ('give_item', 'has_item', 'take_item') and n.get('item') == ident for n in s.get('nodes', []))]
        result.update(library_trainers=library, events=gifts,
                      note='Price applies to every mart selling it and its sell value.')
    else:
        compat = current(project, state, sp.ADDONS, sp.MACHINE_MEMBER)
        species = [s for s in range(1, len(compat) // sp.MACHINE_BYTES)
                   if ident in sp.decode_machines(compat[s * sp.MACHINE_BYTES:(s + 1) * sp.MACHINE_BYTES])]
        from .field_moves import FAMILIES, SURF
        field_moves = {f['move'] for f in FAMILIES.values()} | {SURF['move']}
        result.update(compatible_species=len(species), compatible_examples=species[:12],
                      before_move=_name(project, MOVE_BANK, before), after_move=_name(project, MOVE_BANK, after),
                      field_use=(f"{_name(project, MOVE_BANK, after)} is a field move: field use still needs its "
                                 "badge/authority" if after in field_moves else 'No field use'),
                      note='Compatibility is per TM index: all listed species can now learn the new move from it.')
    return result


def plan(project, context, state, index, operations, label=None):
    """One atomic batch of move/item/machine changes with before-value guards and impact."""
    from .gameplay import qualify
    qualify(project)
    require(isinstance(operations, list) and 1 <= len(operations) <= 32, 'Use 1..32 data operations', 'INVALID_INPUT')
    require(label is None or isinstance(label, str) and 0 < len(label) <= 160, 'Invalid label', 'INVALID_INPUT')
    changes, previews, seen, working = [], [], set(), {}
    machine_now = dict(state.get('machine_moves', {}))
    shop_now = copy.deepcopy(state.get('shops', {}))
    for op in operations:
        require(isinstance(op, dict) and op.get('kind') in ('move', 'item', 'machine', 'shop'),
                'Data operation is move, item, machine or shop', 'INVALID_INPUT')
        kind = op['kind']
        if kind == 'shop':
            from . import field_services as fs
            require(set(op) == {'kind', 'name', 'before', 'items'}, 'Shop fields are name, before and items', 'INVALID_INPUT')
            require(('shop', op['name']) not in seen, 'Duplicate data target in batch', 'DUPLICATE_EDIT')
            seen.add(('shop', op['name']))
            now = shop_now.get(op['name'])
            require(op['before'] == now, 'Shop before-value differs; inspect again', 'BEFORE_VALUE_MISMATCH')
            view = {**state, 'gameplay_members': {**state.get('gameplay_members', {}), **working_members(working)}}
            if op['items'] is None:
                require(now is not None, f"No shop named {op['name']}", 'NOT_FOUND')
                used = fs.users(state, op['name'])
                require(not used, f"Shop {op['name']} is used by {', '.join(used)}; remove those steps first", 'IN_USE')
            else:
                fs.validate_shop(project, view, op['name'], op['items'])
            if op['items'] != now:
                changes.append({'shop': op['name'], 'before': copy.deepcopy(now), 'after': copy.deepcopy(op['items'])})
                if op['items'] is None:
                    shop_now.pop(op['name'])
                else:
                    shop_now[op['name']] = list(op['items'])
            previews.append({'kind': kind, 'name': op['name'], 'before': now, 'after': op['items'],
                             'impact': {'events': fs.users(state, op['name']),
                                        'notes': fs.shop_notes(project, state, op['items'] or []),
                                        'note': 'Every clerk using this shop sells the new list; stock marts are unchanged.'}})
            continue
        if kind == 'machine':
            require(set(op) == {'kind', 'index', 'before', 'move'}, 'Machine fields are index, before and move', 'INVALID_INPUT')
            _int(op['index'], 0, 99, 'Machine index')
            require(op['index'] < TM_COUNT, 'HM mapping stays stock: field authority is keyed to the move', 'UNSUPPORTED_MACHINE')
            require(('machine', op['index']) not in seen, 'Duplicate data target in batch', 'DUPLICATE_EDIT')
            seen.add(('machine', op['index']))
            now = machine_now.get(op['index'], next(m['stock_move'] for m in machines(project, state) if m['id'] == op['index']))
            require(op['before'] == now, 'Machine before-value differs; inspect again', 'BEFORE_VALUE_MISMATCH')
            require(move_entry(project, state, op['move'])['supported'], 'Choose an implemented move', 'UNSUPPORTED_ID')
            if op['move'] != now:
                changes.append({'machine': op['index'], 'before': now, 'after': op['move']})
                machine_now[op['index']] = op['move']
            previews.append({'kind': kind, 'index': op['index'], 'name': f"TM{op['index'] + 1:03d}",
                             'before': now, 'after': op['move'], 'impact': impact(project, state, 'machine', op['index'], now, op['move'])})
            continue
        require(set(op) == {'kind', 'id', 'before_sha256', 'changes'}, f'{kind} fields are id, before_sha256 and changes',
                'INVALID_INPUT')
        path = MOVES if kind == 'move' else ITEMS
        entry = (move_entry if kind == 'move' else item_entry)(project, state, op['id']) if type(op['id']) is int else None
        require(entry is not None and entry['supported'], f"{kind.capitalize()} {op['id']}: {(entry or {}).get('reason') or 'unsupported'}",
                'UNSUPPORTED_ID')
        require((kind, op['id']) not in seen, 'Duplicate data target in batch', 'DUPLICATE_EDIT')
        seen.add((kind, op['id']))
        old = working.get((path, op['id']), _members(project, state, path)(op['id']))
        require(op['before_sha256'] == digest(old), f'{kind.capitalize()} before-value differs; inspect again', 'BEFORE_VALUE_MISMATCH')
        new = encode_move(project, state, old, op['changes']) if kind == 'move' else encode_item(old, op['changes'])
        if kind == 'item' and op['changes'].get('price') == 0:
            selling = sorted(n for n, items in shop_now.items() if op['id'] in items)
            require(not selling, f"Shops {', '.join(selling)} sell this item; a mart item needs a price", 'IN_USE')
        if new != old:
            changes.append({'archive': path, 'member': op['id'], 'before': old.hex(), 'after': new.hex()})
            working[(path, op['id'])] = new
        decode = decode_move if kind == 'move' else decode_item
        previews.append({'kind': kind, 'id': op['id'], 'name': entry['name'], 'expanded': entry['expanded'],
                         'before': decode(old), 'after': decode(new), 'impact': impact(project, state, kind, op['id'])})
    deps = {'baseline': baseline_digest(project), 'catalog_sha256': digest(json.dumps(engine_catalog(), sort_keys=True).encode()),
            'context': authoring.context_ref(context)}
    return {'schema': SCHEMA, 'index': index, 'context': authoring.context_ref(context),
            'request': {'operations': copy.deepcopy(operations), 'label': label},
            'changes': changes, 'preview': previews, 'dependencies': deps,
            'dependencies_sha256': authoring.canonical(deps),
            'label': label or 'Data: ' + ', '.join(sorted({p['kind'] for p in previews}))}


def working_members(working):
    return {(path, member): raw for (path, member), raw in working.items()}


def replay(project, state, t, index):
    try:
        ctx = project.context(header=t['context']['header'], cell=t['context']['cell'])
        expected = plan(project, ctx, state, index, **t['request'])
        require(t == expected, 'Data before-values or dependencies differ', 'BEFORE_VALUE_MISMATCH')
        members = state.setdefault('gameplay_members', {})
        for c in t['changes']:
            if 'shop' in c:
                if c['after'] is None:
                    state.setdefault('shops', {}).pop(c['shop'])
                else:
                    state.setdefault('shops', {})[c['shop']] = list(c['after'])
            elif 'machine' in c:
                state.setdefault('machine_moves', {})[c['machine']] = c['after']
            else:
                members[(c['archive'], c['member'])] = bytes.fromhex(c['after'])
    except (KeyError, TypeError, ValueError, struct.error) as exc:
        raise EditorError('INVALID_INPUT', 'Malformed data transaction') from exc


def runtime(project, state, plan_, layout):
    """Patch the TM table inside overlay 129 (in place: no new resident bytes)."""
    edits = state.get('machine_moves', {})
    if not edits:
        return plan_
    from . import species as sp, character_runtime as cr
    info = cr.overlay(project.blob, 129)
    at = sp.MACHINE_TABLE - info['address']
    for index, move in sorted(edits.items()):
        before = info['data'][at + 2 * index:at + 2 * index + 2]
        after = struct.pack('<H', move)
        if layout is not None:
            layout.patch(sp.MACHINE_TABLE + 2 * index, before, after)
        else:
            files = plan_.setdefault('files', {})
            data = bytearray(files.get(info['file_id'], info['data']))
            require(data[at + 2 * index:at + 2 * index + 2] == before, 'Machine table before-value differs',
                    'BEFORE_VALUE_MISMATCH')
            data[at + 2 * index:at + 2 * index + 2] = after
            files[info['file_id']] = bytes(data)
    return plan_


def summary(t):
    return {'operation': 'data.transaction', 'index': t['index'], 'label': t['label'], 'context': t['context'],
            'preview': [{k: p[k] for k in p if k != 'impact'} for p in t['preview']]}
