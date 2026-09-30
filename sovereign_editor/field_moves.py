"""Field obstacles (Cut trees, Rock Smash rocks, Strength boulders) and typed requirements.

Qualified against the pinned ROM (tests/test_editor_completion.py, evidence under
evidence/editor-completion-v1): stock field-move scripts are member 146 of a/0/1/2
(std 10000 + index). Entry 0 Cut: party move 15 + check_badge 1 (Hive); entry 1 Rock
Smash: move 249 + badge 0 (Zephyr); entry 2 Strength: move 70 + badge 2 (Plain). Stock
objects: sprite 86 / script 10000 (48 in the ROM), sprite 85 / 10001 (103), sprite 84 /
10002 (29). Surf has no script-level badge check: field code gates it (pret
FieldMove_CheckSurf: Fog badge 3 plus a party member with move 57).

An authored obstacle is an ordinary story NPC event whose ``field`` block selects the
family (its stock sprite) and persistence: ``reset`` uses a map-temporary flag (pret
flags 0x01..0x40 are cleared on every map change, as stock Cut trees use), ``permanent``
uses a named on/off state's persistent flag. Its steps choose the authority: the
``require`` step checks a party move, badge, item, named state or money with a clear
refusal branch; ``field_move`` then runs the native action command-for-command as the
ROM's own scripts (party slot, "[mon] used Cut!", follower/animation commands, cry),
sets the object's flag and hides it (Strength instead enables boulder pushing);
``give_badge`` awards a badge (native give_badge) as an authored reward. Collision is
owned by the object itself: its tile must stay walkable ground so removal opens it.
Pure bytes and validation; Project owns writes.
"""
import struct

from .formats import require

FAMILIES = {
    'cut': {'move': 15, 'sprite': 86, 'stock_script': 10000, 'entry': 0, 'badge': 1, 'name': 'Cut',
            'wait': 7, 'anim': 0, 'cry': 12, 'lead_cry': 3, 'prep': True},
    'rock_smash': {'move': 249, 'sprite': 85, 'stock_script': 10001, 'entry': 1, 'badge': 0, 'name': 'Rock Smash',
                   'wait': 10, 'anim': 1, 'cry': 1, 'lead_cry': 2, 'prep': True},
    'strength': {'move': 70, 'sprite': 84, 'stock_script': 10002, 'entry': 2, 'badge': 2, 'name': 'Strength'},
}
SURF = {'move': 57, 'badge': 3, 'script': 10004,
        'authority': 'field code: Fog badge (3) + a party Pokémon knowing Surf (57), facing surfable water'}
# Field-code authority, byte-qualified in this ROM (qualify_field_authority): every call of
# PlayerProfile_TestBadgeFlag (ARM9 0x02028F98) from the party-menu field-move checks, in
# pret sFieldMoveFuncTable order, and the overlay-1 "press A at water" check that returns
# std 10004 only when the facing tile is surfable, badge 3 is set and a party member knows 57.
BADGE_TEST = 0x02028F98
FIELD_MENU_CHECKS = ((0x02067F82, 'cut', 1), (0x02068026, 'fly', 4), (0x020680FA, 'surf', 3),
                     (0x020681DA, 'strength', 2), (0x0206828A, 'rock_smash', 0), (0x02068342, 'waterfall', 7),
                     (0x020683EA, 'rock_climb', 15), (0x02068886, 'whirlpool', 6))
WATER_PROMPT = 0x021E7530      # overlay 1: adds r0,r6; movs r1,#3; bl TestBadge; cmp; beq; ...; movs r1,#57; bl
BADGES = ('Zephyr', 'Hive', 'Plain', 'Fog', 'Storm', 'Mineral', 'Glacier', 'Rising',
          'Boulder', 'Cascade', 'Thunder', 'Rainbow', 'Soul', 'Marsh', 'Volcano', 'Earth')
MAP_TEMP_FLAGS = range(0x01, 0x41)
PERSISTENCE = ('reset', 'permanent')
# Still objects for gates, pickups and shortcuts: stock appearances the ROM's own objects use
# (item ball 87 with the std item-ball scripts, and the three field-move families).
OBJECT_LOOKS = {'ball': 87, 'tree': 86, 'rock': 85, 'boulder': 84}
OPS = {'require', 'field_move', 'give_badge', 'remove'}
REQUIREMENTS = {'move': {'move'}, 'badge': {'badge'}, 'item': {'item', 'count'},
                'state': {'state', 'value'}, 'money': {'amount'}, 'trainer': {'trainer'}}
CHECK_TRAINER_FLAG = 38      # checktrainerflag (flag 0x550 + ID), as the std trainer flow

RESULT, SLOT, FOLLOWER, STATE = 0x800C, 0x8004, 0x8005, 0x8006
LAST_TALKED, LEAD = 0x800D, 0x4000
# Native commands (armips scriptmacros.s of the pinned engine; widths in script-opcodes.json).
WAIT, COMPARE_VARS, SETFLAG, COPYVAR, HIDE, CRY, WAIT_CRY = 3, 18, 30, 42, 101, 76, 77
HAS_MONEY, HAS_ITEM, PARTY_MOVE, EFFECT, PLAYER_STATE, NICK = 112, 128, 141, 183, 187, 199
CHECK_BADGE, GIVE_BADGE, SPECIES, STRENGTH, LEAD_ALIVE = 294, 295, 354, 400, 529
ANIM, PREP, FOLLOWER_INDEX, FIELD_STATE, STRENGTH_DONE, CRY_A, CRY_B, CRY_C = 560, 598, 727, 730, 731, 732, 733, 734


def integer(v, lo, hi): return type(v) is int and not isinstance(v, bool) and lo <= v <= hi


def validate(n, variables, trainers=None):
    """Shape checks (project-independent); returns (fields, targets)."""
    op = n['op']
    if op == 'give_badge':
        require(integer(n.get('badge'), 0, 15), 'Choose a badge 0..15', 'INVALID_EVENT')
        return {'id', 'op', 'badge'}, ['next']
    if op == 'field_move':
        require(n.get('move') in FAMILIES, 'Field move is cut, rock_smash or strength', 'INVALID_EVENT')
        return {'id', 'op', 'move'}, ['yes', 'no']
    if op == 'remove':
        require(isinstance(n.get('target'), str) and n['target'], 'Choose the object to remove (self or its event name)',
                'INVALID_EVENT')
        return {'id', 'op', 'target'}, ['next']
    kind = n.get('kind')
    require(kind in REQUIREMENTS, 'Requirement kind is move, badge, item, state, money or trainer', 'INVALID_EVENT')
    if kind == 'move':
        require(integer(n.get('move'), 1, 1023), 'Choose the move a party Pokémon must know', 'INVALID_EVENT')
    elif kind == 'badge':
        require(integer(n.get('badge'), 0, 15), 'Choose a badge 0..15', 'INVALID_EVENT')
    elif kind == 'item':
        require(integer(n.get('item'), 1, 1023) and integer(n.get('count'), 1, 99), 'Choose an item and count 1..99',
                'INVALID_EVENT')
    elif kind == 'money':
        require(integer(n.get('amount'), 1, 999999), 'Money requirement is 1..999999', 'INVALID_EVENT')
    elif kind == 'trainer':
        require(n.get('trainer') in (trainers or {}), 'Choose a library trainer that must be defeated', 'INVALID_EVENT')
    else:
        require(n.get('state') in variables, 'Choose a named persistent state', 'INVALID_EVENT')
        require(integer(n.get('value'), 0, 65535), 'State value must be 0..65535', 'INVALID_EVENT')
        require(not variables[n['state']].get('switch') or n['value'] in (0, 1),
                'An on/off state takes 0 (off) or 1 (on)', 'INVALID_EVENT')
    return {'id', 'op', 'kind'} | REQUIREMENTS[kind], ['yes', 'no']


def messages(n):
    if n['op'] != 'field_move':
        return []
    name = FAMILIES[n['move']]['name']
    if n['move'] == 'strength':
        return ['[mon] used Strength!', 'Boulders can now be\npushed around!',
                'Strength made it possible\nto move boulders around.']
    return [f'[mon] used {name}!']


def qualify(project, n):
    from . import gameplay
    if n['op'] == 'require' and n['kind'] == 'move':
        gameplay.valid_id(project, 'moves', n['move'])
    elif n['op'] == 'require' and n['kind'] == 'item':
        require(bool(gameplay.name(project, 222, n['item'])), f"Unknown item {n['item']}", 'UNSUPPORTED_ID')
    qualify_runtime(project)


def qualify_field_authority(blob):
    """Badge per native field action, read from this ROM's code (not from pret)."""
    from .formats import arm9_code
    from . import character_runtime as cr

    def bl_target(data, base, address):
        hi, lo = struct.unpack_from('<HH', data, address - base)
        require(hi >> 11 == 0x1E and lo >> 11 == 0x1F, f'No call at {address:#x}', 'UNSUPPORTED_RUNTIME')
        off = ((hi & 0x7FF) << 12) | ((lo & 0x7FF) << 1)
        return address + 4 + (off - 0x800000 if off & 0x400000 else off)

    arm = arm9_code(blob)
    rows = []
    for address, action, badge in FIELD_MENU_CHECKS:
        require(bl_target(arm, 0x02000000, address) == BADGE_TEST
                and struct.unpack_from('<H', arm, address - 2 - 0x02000000)[0] == 0x2100 | badge,
                f'Field-move badge check for {action} differs from the qualified build', 'UNSUPPORTED_RUNTIME')
        rows.append({'action': action, 'badge': badge, 'badge_name': BADGES[badge], 'call': address})
    start, size = struct.unpack_from('<II', blob, 0x50)
    entry = next(e for e in (struct.unpack_from('<8I', blob, o) for o in range(start, start + size, 32)) if e[0] == 1)
    require(entry[7] >> 24 == 0, 'Field overlay is compressed', 'UNSUPPORTED_RUNTIME')
    data, base = bytes(cr.file_by_id(blob, entry[6])[1]), entry[1]
    code = struct.unpack_from('<2H', data, WATER_PROMPT - base) + struct.unpack_from('<H', data, WATER_PROMPT + 0x12 - base)
    require(code == (0x1C30, 0x2103, 0x2139) and bl_target(data, base, WATER_PROMPT + 4) == BADGE_TEST
            and struct.unpack_from('<I', data, 0x021E7614 - base)[0] == SURF['script'],
            'Surf water prompt differs from the qualified build', 'UNSUPPORTED_RUNTIME')
    return {'field_menu': rows, 'water_prompt': {'overlay': 1, 'address': WATER_PROMPT, 'badge': 3, 'move': 57,
                                                 'script': SURF['script']}}


def qualify_runtime(project):
    """The stock family scripts in this ROM check the recorded move and badge."""
    if getattr(project, '_field_moves_qualified', False):
        return
    from . import script_disasm, dialogue_format as fmt
    raw = project.resource(fmt.SCRIPT_ARCHIVE, 146)[1]
    starts = script_disasm.entries(raw)
    for family, spec in FAMILIES.items():
        listing = script_disasm.disassemble(raw, [starts[spec['entry']]])
        moves = {c[2][1] for c in listing.values() if c[0] == PARTY_MOVE}
        badges = {c[2][0] for c in listing.values() if c[0] == CHECK_BADGE}
        require(moves == {spec['move']} and badges == {spec['badge']},
                f'Stock {spec["name"]} script differs from the qualified move/badge rule', 'UNSUPPORTED_RUNTIME')
    # Each family appearance is the one the ROM's own objects pair with its stock script.
    import ndspy.narc
    from . import world
    from .formats import file_span
    pairs = set()
    for member in ndspy.narc.NARC(bytes(file_span(project.blob, world.EVENT_ARCHIVE)[1])).files:
        at = 4 + struct.unpack_from('<I', member)[0] * 20
        count = struct.unpack_from('<I', member, at)[0]
        for i in range(count):
            sprite_id, script = struct.unpack_from('<H', member, at + 4 + 32 * i + 2)[0], \
                struct.unpack_from('<H', member, at + 4 + 32 * i + 10)[0]
            pairs.add((sprite_id, script))
    for spec in FAMILIES.values():
        require((spec['sprite'], spec['stock_script']) in pairs,
                f"No stock {spec['name']} object uses sprite {spec['sprite']}", 'UNSUPPORTED_RUNTIME')
    project._field_moves_qualified = True


def compile_node(c, n, page, labels, spec, variables, env=None, trainers=None):
    """Emit one step; ``page(i)`` prints this step's i-th allocated message and waits."""
    op = n['op']
    if op == 'give_badge':
        c.emit('2H', GIVE_BADGE, n['badge']); c.jump(labels['next']); return
    if op == 'remove':
        target = spec if n['target'] == 'self' else (env or {}).get('actors', {}).get(n['target'])
        require(target is not None and target.get('field_flag'), 'Removal needs a hideable object in this area',
                'INVALID_EVENT')
        c.emit('2H', SETFLAG, target['field_flag'])
        c.emit('2H', HIDE, LAST_TALKED if n['target'] == 'self' else target['npc_id'])
        c.jump(labels['next']); return
    if op == 'require':
        kind = n['kind']
        if kind == 'move':
            c.emit('3H', PARTY_MOVE, RESULT, n['move']); c.compare(RESULT, 6); c.jump(labels['no'], 1)
        elif kind == 'badge':
            c.emit('3H', CHECK_BADGE, n['badge'], RESULT); c.compare(RESULT, 0); c.jump(labels['no'], 1)
        elif kind == 'item':
            c.emit('4H', HAS_ITEM, n['item'], n['count'], RESULT); c.compare(RESULT, 0); c.jump(labels['no'], 1)
        elif kind == 'money':
            c.emit('HHI', HAS_MONEY, RESULT, n['amount']); c.compare(RESULT, 0); c.jump(labels['no'], 1)
        elif kind == 'trainer':
            # Defeated = its named defeat state (talk battles) or the native trainer flag.
            trainer = trainers[n['trainer']]
            if trainer.get('defeat_state'):
                from .scene_commands import state_jump
                state_jump(c, variables[trainer['defeat_state']], 1, labels['yes']); c.jump(labels['no']); return
            c.emit('2H', CHECK_TRAINER_FLAG, trainer['trainer_id']); c.jump(labels['yes'], 1); c.jump(labels['no']); return
        else:
            from .scene_commands import state_jump
            state_jump(c, variables[n['state']], n['value'], labels['yes']); c.jump(labels['no']); return
        c.jump(labels['yes']); return
    family = FAMILIES[n['move']]
    field = spec.get('field') or {}
    require(field.get('family') == n['move'], 'A field move step belongs to an obstacle of the same family',
            'INVALID_EVENT')
    tag = '$field' + n['id']
    if n['move'] == 'strength':
        c.emit('HBH', STRENGTH, 2, RESULT); c.compare(RESULT, 1); c.jump(tag + 'active', 1)
    c.emit('3H', PARTY_MOVE, RESULT, family['move']); c.compare(RESULT, 6); c.jump(labels['no'], 1)
    if n['move'] == 'strength':
        c.emit('HB', STRENGTH, 1)
    c.emit('3H', COPYVAR, SLOT, RESULT)
    if n['move'] != 'strength':
        c.emit('2H', FOLLOWER_INDEX, FOLLOWER)
    c.emit('HBH', NICK, 0, SLOT); page(0)
    if n['move'] == 'strength':
        c.emit('2H', FOLLOWER_INDEX, FOLLOWER)
    c.emit('2H', PLAYER_STATE, RESULT); c.emit('2H', FIELD_STATE, STATE)
    # Stock branch order: bike/surf state 2, state 1, follower is the user, follower hidden.
    effect = tag + 'effect'
    c.compare(RESULT, 2); c.jump(effect, 1)
    c.compare(RESULT, 1); c.jump(effect, 1)
    c.emit('3H', COMPARE_VARS, SLOT, FOLLOWER); c.jump(effect, 5)
    c.compare(STATE, 1); c.jump(effect, 1)
    # The follower performs the move: the lead's cry via the stock cry routine.
    if family.get('prep'):
        c.emit('2H', PREP, 1)
    c.emit('2H', LEAD_ALIVE, LEAD); c.emit('3H', SPECIES, LEAD, LEAD)
    if n['move'] != 'strength':
        c.emit('HB', CRY_A, 20)
    cry = family.get('cry', 0)
    c.emit('HBH', CRY_B, cry, RESULT); c.compare(RESULT, 1); c.jump(tag + 'cry1', 5)
    c.emit('HB', CRY_C, 2); c.emit('HB', CRY_A, 1); c.jump(tag + 'cried')
    c.label(tag + 'cry1'); c.emit('HB', CRY_C, 1)
    c.label(tag + 'cried'); c.emit('3H', CRY, LEAD, 0); c.emit('H', WAIT_CRY)
    if n['move'] == 'strength':
        c.emit('H', STRENGTH_DONE); c.jump(tag + 'shown')
    else:
        c.emit('3H', ANIM, family['lead_cry'], FOLLOWER); c.jump(tag + 'hide')
    c.label(effect)
    c.emit('2H', EFFECT, SLOT)
    if n['move'] != 'strength':
        c.emit('3H', ANIM, family['anim'], FOLLOWER)
        c.label(tag + 'hide')
        c.emit('3H', WAIT, family['wait'], RESULT)
        # The flag hides the object on every later spawn (temporary: until the next
        # map change; permanent: the named state stays on).
        c.emit('2H', SETFLAG, spec['field_flag']); c.emit('2H', HIDE, LAST_TALKED)
        c.label(tag + 'anim'); c.emit('3H', WAIT, 1, RESULT)
        c.compare(FOLLOWER, 0); c.jump(tag + 'anim', 1)
        c.jump(labels['yes']); return
    c.label(tag + 'shown'); page(1); c.jump(labels['yes'])
    c.label(tag + 'active'); page(2); c.jump(labels['yes'])


def plan_field(project, context, state, key, value, after, before):
    """Validate an obstacle's ``field`` block and allocate its hide flag."""
    from . import event_authoring as ev, storage, dialogue_format as fmt, script_disasm
    field = value['field']
    require(isinstance(field, dict) and (field.get('family') in FAMILIES or field.get('family') == 'object')
            and field.get('persistence') in PERSISTENCE,
            'Field objects choose a family (cut, rock_smash, strength, object) and reset/permanent persistence',
            'INVALID_INPUT')
    obj = field['family'] == 'object'
    require(set(field) == {'family', 'persistence'} | ({'state'} if field['persistence'] == 'permanent' else set())
            | ({'look'} if obj else set()), 'Field fields are family, persistence, (permanent) state and (object) look',
            'INVALID_INPUT')
    require(not obj or field['look'] in OBJECT_LOOKS, f"Object look is one of {', '.join(OBJECT_LOOKS)}", 'INVALID_INPUT')
    require(value['kind'] == 'npc' and value['character'] is None and value.get('stock_sprite') is None
            and value['movement'] == 0 and value['range_x'] == 0 and value['range_z'] == 0
            and not value.get('presence') and not value['once_state'],
            'A field obstacle is a still NPC with its family appearance and no presence or one-time state',
            'INVALID_INPUT')
    moves = [n for n in value['nodes'] if n['op'] == 'field_move']
    if obj:
        require(not moves, 'Plain objects have no field move step; use a remove step', 'INVALID_EVENT')
    else:
        require(moves and all(n['move'] == field['family'] for n in moves),
                f"A {FAMILIES[field['family']]['name']} obstacle needs its own field move step", 'INVALID_EVENT')
    qualify_runtime(project)
    if field['family'] == 'strength':
        # Boulders are pushed, never hidden; the engine re-creates them at their event
        # tile on every map load, which is the puzzle reset.
        require(field['persistence'] == 'reset', 'Strength boulders reset on map load; they cannot be permanent',
                'INVALID_INPUT')
        after['field_flag'] = 0
        return {'field_script': FAMILIES['strength']['stock_script']}
    if field['persistence'] == 'permanent':
        states = state.get('story', {}).get('state', {})
        require(field.get('state') in states and storage.is_switch(states[field['state']]),
                'Permanent removal needs a named on/off state', 'INVALID_INPUT')
        flag = states[field['state']]['flag']
        for other_key, other in state.get('story', {}).get('sequence', {}).items():
            require(other_key == key or (other.get('field') or {}).get('state') != field['state'],
                    'Each permanent obstacle needs its own on/off state', 'STATE_CONFLICT')
    else:
        member = context['event_member']
        used = {r['flag'] for r in ev.records(ev.base(project, member)) if r['kind'] == 'npc'}
        raw = project.resource(fmt.SCRIPT_ARCHIVE, context['header']['script_file'])[1]
        for op, _, args, _ in script_disasm.disassemble(raw).values():
            if op in (30, 31, 32) and args:
                used.add(args[0])
        for other_key, other in state.get('story', {}).get('sequence', {}).items():
            if other.get('event_member') == member and other_key != key and other.get('field_flag'):
                used.add(other['field_flag'])
        kept = (before or {}).get('field_flag')
        if kept in MAP_TEMP_FLAGS and kept not in used and (before.get('field') or {}).get('persistence') == 'reset':
            flag = kept
        else:
            free = [f for f in MAP_TEMP_FLAGS if f not in used]
            require(free, 'No free map-temporary flag in this area', 'RESOURCE_CAPACITY')
            flag = free[0]
    after['field_flag'] = flag
    return {'field_script': FAMILIES[field['family']]['stock_script']} if not obj else {}


def sprite(spec):
    field = spec['field']
    return OBJECT_LOOKS[field['look']] if field['family'] == 'object' else FAMILIES[field['family']]['sprite']


def check_targets(state, key, spec):
    """Every remove step names a hideable object (not a boulder) in the same event file."""
    sequences = state.get('story', {}).get('sequence', {})
    for n in spec['nodes']:
        if n['op'] != 'remove':
            continue
        target = spec if n['target'] == 'self' else sequences.get(n['target'])
        require(target is not None and n['target'] != key, f"Remove step {n['id']}: no object {n['target']}"
                if n['target'] != key else 'Use target self for the object itself', 'INVALID_EVENT')
        require(target.get('field') and target['field']['family'] != 'strength'
                and target.get('event_member') == spec.get('event_member'),
                f"Remove step {n['id']}: {n['target']} is not a removable object in this area", 'INVALID_EVENT')
