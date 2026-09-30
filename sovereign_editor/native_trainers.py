"""Native sight trainers (BATTLE-01/02): the stock trainer object, flow, messages and flag.

Qualified against the pinned ROM (see tests and evidence/editor-completion-v1):
* A stock trainer is an object record with type 1, sight range in param 0 (stock 0..7,
  mostly 2..5) and script 3000 + trainer - 1 (FIRST_TRAINER_INDEX 1); the second object of
  a double pair uses 5000 + trainer - 1 (pret ScriptNumToTrainerNum). Both run common script
  bank 953, whose 739 first entries all start the same std trainer flow (intro message,
  double check with party_check_for_double, trainer_battle, win check, settrainerflag;
  a loss whites out). Entry 739 has another target, so trainer 740 is never native.
  Authored trainer IDs beyond the stock table get entries aliased to that flow; every
  existing entry and every script byte is kept (dialogue_format.alias_scripts).
* Messages: trtblofs (a/1/3/1) holds each trainer's byte offset into trtbl (a/0/5/7),
  whose 4-byte records {trainer, type} are also the text index in bank 728. Singles use
  types 0 intro, 1 defeat (shown in battle), 2 after-battle; doubles use 3..6 for the
  first object and 7..10 for the second (intro, defeat, after, not enough Pokémon).
* Intro messages (types 0, 3, 7) end with the stock page break, like all 380 stock single
  intros and the double intros: the text waits for a press before the battle starts
  (R82-TEXT-01; without it each intro page ran straight into the next page or battle).
* Defeat persists in the trainer flag 0x550 + ID (authored IDs 738..801 stay below the
  named-state region 0x872). A defeated trainer only repeats its after-battle line.

The object owns its tile; its sight line must be clear walkable ground free of other
objects, entrances and step-on triggers so the native approach never crosses a prop.
Pure planning/bytes; Project owns writes.
"""
import struct

from .formats import require

TYPE_TRAINER, STD_TRAINER, STD_PARTNER, BANK = 1, 3000, 5000, 953
SPECIAL_TRAINER = 740            # bank 953 entry 739 is not the std flow
TRTBL, TRTBL_OFFSETS, TRAINER_TEXT = 'a/0/5/7', 'a/1/3/1', 728
SINGLE_TYPES = {'before': 0, 'defeat': 1, 'revisit': 2}
DOUBLE_TYPES = ({'before': 3, 'defeat': 4, 'revisit': 5, 'insufficient': 6},
                {'before': 7, 'defeat': 8, 'revisit': 9, 'insufficient': 10})
DEFAULT_TEXT = {'defeat': ['You win.'], 'insufficient': ['You need two Pokémon\nready to battle us!']}
SIGHT = (1, 7)
MOVEMENTS = (0, 2)               # stand still facing, look around (qualified npc behaviors)
STEP = {0: (0, -1), 1: (0, 1), 2: (-1, 0), 3: (1, 0)}
FIELDS = {'trainer', 'sight', 'partner'}


def is_native(spec):
    return spec.get('kind') == 'trainer'


def script(trainer, partner):
    return (STD_PARTNER if partner else STD_TRAINER) + trainer['trainer_id'] - 1


def plan(project, context, state, value, trainers):
    """Validate a native placement request (the story plan adds context and IDs)."""
    from . import trainer_format as tf
    require(set(value) >= FIELDS, 'A trainer placement names its trainer, sight range and partner flag', 'INVALID_INPUT')
    trainer = trainers.get(value['trainer'])
    require(trainer is not None and trainer.get('policy') == tf.POLICY,
            'Place an ordinary v2 trainer (its team, texts and battle type) from the library', 'INVALID_INPUT')
    require(trainer['trainer_id'] != SPECIAL_TRAINER, 'Trainer slot 740 cannot run the native trainer flow; '
            'use another library trainer', 'UNSUPPORTED_RUNTIME')
    require(type(value['sight']) is int and SIGHT[0] <= value['sight'] <= SIGHT[1], 'Sight range is 1..7 tiles',
            'INVALID_INPUT')
    require(type(value['partner']) is bool, 'partner is true or false', 'INVALID_INPUT')
    require(not value['partner'] or trainer.get('battle') == 'double', 'Only a double-battle trainer has a partner object',
            'INVALID_INPUT')
    require(value['movement'] in MOVEMENTS, 'Trainers stand still or look around', 'INVALID_INPUT')
    require(value['nodes'] == [] and value['once_state'] is None and value['range_x'] == value['range_z'] == 0,
            'A native trainer runs the stock flow: no steps, one-time state or movement range', 'INVALID_INPUT')
    if trainer['character']:
        require(value['character'] in (None, trainer['character']) and value.get('stock_sprite') is None,
                "The trainer object uses the trainer's imported character", 'INVALID_INPUT')
    else:
        require(value.get('stock_sprite') is not None and value['character'] is None,
                'Choose a stock appearance for a stock-class trainer', 'INVALID_INPUT')


def record(spec, trainer, sprite):
    r = bytearray(32)
    struct.pack_into('<6Hh', r, 0, spec['npc_id'], sprite, spec['movement'], TYPE_TRAINER, 0,
                     script(trainer, spec['partner']), spec['facing'])
    struct.pack_into('<H', r, 14, spec['sight'])
    struct.pack_into('<2Hi', r, 24, spec['x'], spec['z'], spec['y'])
    return bytes(r)


def sight_tiles(spec):
    directions = range(4) if spec['movement'] == 2 else [spec['facing']]
    return {d: [(spec['x'] + dx * k, spec['z'] + dz * k) for k in range(1, spec['sight'] + 1)]
            for d in directions for dx, dz in [STEP[d]]}


def validate(project, state, key, spec, rows, trainers, placements):
    """Sight lines clear and walkable; double trainers placed as one pair in one area."""
    from . import world
    from .border_authoring import cell_of
    from .formats import EditorError
    header = spec['context']['header']
    blockers = {}
    for r in rows:
        if r['kind'] == 'npc' and (r['x'], r['z']) != (spec['x'], spec['z']):
            blockers[(r['x'], r['z'])] = f"object {r['id']}"
        elif r['kind'] == 'warp':
            blockers[(r['x'], r['z'])] = f"entrance {r['id']}"
        elif r['kind'] == 'trigger':
            for x in range(r['x'], r['x'] + r['width']):
                for z in range(r['z'], r['z'] + r['height']):
                    blockers[(x, z)] = f"step-on trigger {r['id']}"
    for direction, tiles in sight_tiles(spec).items():
        for x, z in tiles:
            try:
                cx, cy = cell_of(project, header, x, z)
            except EditorError as exc:
                raise EditorError('INVALID_SIGHT', f'Trainer {key}: sight line leaves the area at {x},{z}') from exc
            ctx = project.context(header=header, cell=[cx, cy])
            offset = world.cell_offset(ctx, x, z)
            pair = state['permissions'].get((ctx['map_member'], offset),
                                            project.member_raw(ctx['map_member'])[offset:offset + 2])
            require(not world.is_blocked(pair) and pair[0] not in (0x38, 0x39, 0x3A, 0x3B),
                    f'Trainer {key}: sight tile {x},{z} is not walkable ground', 'INVALID_SIGHT')
            require((x, z) not in blockers, f'Trainer {key}: sight tile {x},{z} holds {blockers.get((x, z))}',
                    'INVALID_SIGHT')
    trainer = trainers[spec['trainer']]
    same = [s for s in placements if s['trainer'] == spec['trainer']]
    if trainer.get('battle') == 'double':
        require(sorted(s['partner'] for s in same) == [False, True]
                and len({s['event_member'] for s in same}) == 1,
                f"Double trainer {spec['trainer']} needs one object and one partner object in one area", 'INVALID_EVENT')
        first, second = same
        require(abs(first['x'] - second['x']) + abs(first['z'] - second['z']) == 1,
                f"The two objects of {spec['trainer']} stand side by side", 'INVALID_EVENT')
    else:
        require(len(same) == 1, f"Trainer {spec['trainer']} is placed once", 'INVALID_EVENT')


def tables(project, state, trainers, placements):
    """{archive: {member: bytes}} for the native trainers' messages and std entries."""
    from . import dialogue_format as fmt
    from .formats import resource
    placed = {s['trainer'] for s in placements}
    if not placed:
        return {}
    table = bytearray(resource(project.blob, TRTBL, 0)[1])
    offsets = resource(project.blob, TRTBL_OFFSETS, 0)[1]
    stock_count = len(offsets) // 2
    messages, starts = [], {}
    for key, t in trainers.items():
        if key not in placed:
            continue
        starts[t['trainer_id']] = len(table)
        kinds = DOUBLE_TYPES if t.get('battle') == 'double' else (SINGLE_TYPES,)
        for types in kinds:
            for field, kind in types.items():
                table += struct.pack('<HH', t['trainer_id'], kind)
                pages = list(t.get(field) or DEFAULT_TEXT[field])
                messages.append(fmt.Held(pages) if field == 'before' else pages)
    last = max(t['trainer_id'] for t in trainers.values())
    ids = range(stock_count, last + 1)
    extra = struct.pack('<' + 'H' * len(ids), *[starts.get(i, len(table)) for i in ids])
    text = resource(project.blob, fmt.TEXT_ARCHIVE, TRAINER_TEXT)[1]
    require(len(fmt.text_entries(text)[1]) * 4 == len(table) - 4 * len(messages),
            'Trainer message table and text bank differ', 'BEFORE_VALUE_MISMATCH')
    bank = resource(project.blob, fmt.SCRIPT_ARCHIVE, BANK)[1]
    return {TRTBL: {0: bytes(table)}, TRTBL_OFFSETS: {0: offsets + extra},
            fmt.TEXT_ARCHIVE: {TRAINER_TEXT: fmt.append_messages(text, messages, limit=65535)},
            fmt.SCRIPT_ARCHIVE: {BANK: fmt.alias_scripts(bank, max(s for s in (t['trainer_id'] for k, t in trainers.items()
                                                                              if k in placed)))}}
