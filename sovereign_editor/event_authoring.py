"""Existing HGSS events: guarded fixed records, shared by Project/UI/CLI.

Layout checked against DSPRE EventFile.cs (pinned in references/dspre) and
pret/pokeheartgold include/map_events_internal.h. In particular BgEvent X/Z
are signed 32-bit words; direction has a preserved trailing padding halfword.
No scripts, sprite assignments, record counts, heights or unknown fields change.
"""
import copy
import struct

from . import authoring, world
from .formats import EditorError, digest, events, require, resource

SCHEMA = 'sovereign-event-transaction-v1'
KINDS = {'npc': ('npcs', 32), 'background': ('backgrounds', 20), 'warp': ('warps', 12)}
FIELDS = {
    'npc': {'x': (24, 'H', 65535), 'z': (26, 'H', 65535), 'facing': (12, 'h', 3),
            'range_x': (20, 'h', 31), 'range_z': (22, 'h', 31)},
    'background': {'x': (4, 'i', 65535), 'z': (8, 'i', 65535)},
    'warp': {'x': (0, 'H', 65535), 'z': (2, 'H', 65535),
             'destination': (4, 'H', 65535), 'destination_warp': (6, 'H', 65535)},
}


def is_transaction(value):
    return isinstance(value, dict) and value.get('schema') == SCHEMA


def base(project, member):
    if not hasattr(project, '_event_cache'):
        project._event_cache = {}
    if member not in project._event_cache:
        project._event_cache[member] = resource(project.blob, world.EVENT_ARCHIVE, member)[1]
    return project._event_cache[member]


def records(raw):
    decoded = events(raw)
    cursor, result = 0, []
    for kind, section, size in [('background', 'backgrounds', 20), ('npc', 'npcs', 32),
                                ('warp', 'warps', 12), ('trigger', 'triggers', 16)]:
        count = struct.unpack_from('<I', raw, cursor)[0]
        cursor += 4
        for slot in range(count):
            value = dict(decoded[section][slot])
            value.pop('offset', None)
            record = raw[cursor:cursor + size]
            if kind == 'background':
                value['background_type'] = value['kind']
            value.update(kind=kind, slot=slot, record_offset=cursor, raw=record)
            if kind == 'npc':
                value.update(facing=struct.unpack_from('<h', record, 12)[0],
                             range_x=struct.unpack_from('<h', record, 20)[0],
                             range_z=struct.unpack_from('<h', record, 22)[0])
            if kind == 'background':
                value['direction'] = struct.unpack_from('<H', record, 16)[0]
            if kind == 'warp':
                value['y'] = struct.unpack_from('<I', record, 8)[0]
            result.append(value)
            cursor += size
    return result


def raw_member(project, member, state):
    raw = bytearray(base(project, member))
    for (m, offset), value in state['event_records'].items():
        if m == member:
            raw[offset:offset + len(value)] = value
    from .simple_interactions import append_events
    from . import story_authoring
    return story_authoring.append_events(project, member, state, append_events(project, member, state, bytes(raw)))


def lookup(project, member, kind, event_id, state):
    require(kind in KINDS and type(event_id) is int, 'Choose an NPC, background or warp ID', 'INVALID_INPUT')
    found = next((r for r in records(raw_member(project, member, state))
                  if r['kind'] == kind and r['id'] == event_id), None)
    require(found is not None, f'No {kind} {event_id} in event member {member}', 'NOT_FOUND')
    return found


def initialise(project, state, positions):
    require(isinstance(positions, dict), 'Invalid NPC positions')
    require(set(positions).issubset({'1'}), 'This release only qualifies movement of NPC 1', 'UNSUPPORTED_EDIT')
    for position in positions.values():
        require(isinstance(position, dict) and set(position) == {'x', 'z'}, 'Invalid position fields')
        require(all(type(v) is int and 0 <= v <= 65535 for v in position.values()),
                'Coordinates must be integer tiles in 0..65535')
    state['event_records'] = {}
    state['event_npcs'] = {}
    for npc in project.base_events['npcs']:
        if str(npc['id']) in positions:
            offset = npc['offset'] - 24
            record = bytearray(base(project, 64)[offset:offset + 32])
            target = positions[str(npc['id'])]
            struct.pack_into('<2H', record, 24, target['x'], target['z'])
            state['event_records'][(64, offset)] = bytes(record)
    state['event_initial'] = dict(state['event_records'])


def store(project, state, member, record):
    original = next((r for r in records(base(project, member))
                     if (r['kind'], r['id']) == (record['kind'], record['id'])), None)
    require(original is not None, 'Use the simple interaction editor for authored events', 'BOUND_EVENT')
    state['event_records'][(member, original['record_offset'])] = record['raw']
    if record['kind'] == 'background':
        state['interactions'][(member, record['id'])] = record['raw']


def location(project, header, x, z):
    return project.context(header=header, cell=[x // 32, z // 32])


def endpoint(project, header, event_id, state):
    head = world.read_header(project.blob, header, project.arm9)
    record = lookup(project, head['event_file'], 'warp', event_id, state)
    context = location(project, header, record['x'], record['z'])
    return context, record


def public(record):
    return {k: v for k, v in record.items() if k not in ('raw', 'record_offset')}


def connection(project, source_header, record, state):
    try:
        context, target = endpoint(project, record['destination'], record['destination_warp'], state)
        return {'resolved': True, 'header': context['header']['id'], 'name': context['name'],
                'cell': [context['cell']['x'], context['cell']['y']], 'event_member': context['event_member'],
                'event': public(target),
                'returns_to_source': (target['destination'], target['destination_warp']) == (source_header, record['id'])}
    except EditorError as exc:
        return {'resolved': False, 'reason': str(exc), 'header': record['destination'],
                'warp': record['destination_warp']}


def association(context, record):
    if record['kind'] == 'npc':
        return {'type': 'overworld-sprite', 'sprite': record['sprite'],
                'note': 'Sprite assignment is preserved; this is not a scenery model.'}
    if context['header']['id'] == 60 and record['kind'] == 'background' and record['id'] == 2:
        return {'type': 'identified-model', 'object_id': 'baseline:0:13', 'map_member': 0, 'slot': 13,
                'note': 'New Bark town sign. Moving this interaction does not move its model or collision.'}
    return {'type': 'unbound', 'note': 'No verified model association. Model and collision stay in place.'}


def view(project, context, state):
    member = context['event_member']
    original = {(r['kind'], r['id']): r['raw'] for r in records(base(project, member))}
    result = []
    ox, oz = context['origin']
    for record in records(raw_member(project, member, state)):
        value = public(record)
        value.update(in_cell=ox <= record['x'] < ox + 32 and oz <= record['z'] < oz + 32,
                     changed=record['raw'] != original.get((record['kind'], record['id'])),
                     editable_fields=list(FIELDS.get(record['kind'], {})) if (record['kind'],record['id']) in original else [],
                     association=association(context, record))
        if (record['kind'],record['id']) not in original:
            value['association']['note'] = 'Authored simple interaction. Use Author area → Dialogue to edit its text or position, duplicate it, or remove it.'
        if record['kind'] == 'warp':
            value['connection'] = connection(project, context['header']['id'], record, state)
        result.append(value)
    head = context['header']
    if not hasattr(project, '_event_users'):
        project._event_users = {}
        for i in range(world.header_count(project.blob)):
            h = world.read_header(project.blob, i, project.arm9)
            project._event_users.setdefault(h['event_file'], []).append(i)
    return {'context': authoring.context_ref(context), 'member': member,
            'shared_headers': project._event_users[member], 'events': result,
            'script_file': head['script_file'], 'text_archive': head['text_archive'],
            'scope': 'Existing records only. Scripts, text, movement behavior, sprites, height and unknown fields preserved.'}


def dependencies(project, contexts, index):
    result = []
    for context in sorted({c['id']: c for c in contexts}.values(), key=lambda c: c['id']):
        head = context['header']
        refs = [(world.EVENT_ARCHIVE, head['event_file']), ('a/0/1/2', head['script_file']),
                ('a/0/1/2', head['level_script']), ('a/0/2/7', head['text_archive'])]
        result.append({'context': authoring.dependencies(context, index),
                       'resources': [{'archive': a, 'member': m, 'sha256': digest(resource(project.blob, a, m)[1])}
                                     for a, m in refs]})
    return result


def validate_npc(project, context, record, state):
    rx, rz = record['range_x'], record['range_z']
    require(0 <= rx <= 31 and 0 <= rz <= 31, 'NPC movement range must be 0..31 tiles', 'UNSUPPORTED_RANGE')
    others = records(raw_member(project, context['event_member'], state))
    initial = records(raw_member(project, context['event_member'], {'event_records': state['event_initial']}))
    original = next(r for r in initial if r['kind'] == 'npc' and r['id'] == record['id'])
    def in_original(x, z):
        return (abs(x - original['x']) <= max(0, original['range_x'])
                and abs(z - original['z']) <= max(0, original['range_z']))
    def intersects(other, x, z):
        if other['kind'] == 'npc':
            return abs(x - other['x']) <= max(0, other['range_x']) and abs(z - other['z']) <= max(0, other['range_z'])
        if other['kind'] == 'warp':
            return x == other['x'] and other['z'] <= z <= other['z'] + 1
        return (other['x'] <= x < other['x'] + other['width'] and other['z'] <= z < other['z'] + other['height'])
    for x in range(record['x'] - rx, record['x'] + rx + 1):
        for z in range(record['z'] - rz, record['z'] + rz + 1):
            offset = world.cell_offset(context, x, z)
            raw = project.member_raw(context['map_member'])
            pair = state['permissions'].get((context['map_member'], offset), raw[offset:offset + 2])
            require(not world.is_blocked(pair) or (in_original(x, z) and world.is_blocked(raw[offset:offset + 2])),
                    'NPC movement range intersects blocked terrain/water', 'BLOCKED_TILE')
            for other in others:
                if other['kind'] not in ('npc', 'warp', 'trigger') or (other['kind'] == 'npc' and other['id'] == record['id']):
                    continue
                old = next((r for r in initial if (r['kind'], r['id']) == (other['kind'], other['id'])), None)
                # Stock ranges often brush collision or conditional actors. Preserve
                # those exact existing intersections, but reject newly introduced ones.
                require(not intersects(other, x, z) or (old is not None and in_original(x, z) and intersects(old, x, z)),
                        'NPC movement range introduces an actor, warp approach or script-trigger overlap', 'EVENT_CONFLICT')


def plan(project, context, state, index, kind, event_id, values, reciprocal=False, label=None):
    require(kind in KINDS, 'Only NPC, background and warp records are editable', 'UNSUPPORTED_EDIT')
    require(isinstance(values, dict) and set(values).issubset(FIELDS[kind]),
            'Unsupported event fields; scripts, movement behavior, sprites and height are preserved', 'UNSUPPORTED_EDIT')
    require(type(reciprocal) is bool and (not reciprocal or kind == 'warp'), 'Reciprocal links require a warp')
    require(label is None or (isinstance(label, str) and 0 < len(label) <= 160), 'Use a label of 1..160 characters')
    member = context['event_member']
    source = lookup(project, member, kind, event_id, state)
    world.cell_offset(context, source['x'], source['z'])
    after = bytearray(source['raw'])
    for field, value in values.items():
        offset, fmt, maximum = FIELDS[kind][field]
        require(type(value) is int and 0 <= value <= maximum, f'{field} must be an integer in 0..{maximum}', 'INVALID_INPUT')
        struct.pack_into('<' + fmt, after, offset, value)
    trial = {**state, 'event_records': dict(state['event_records']), 'interactions': dict(state['interactions'])}
    store(project, trial, member, {**source, 'raw': bytes(after)})
    target = lookup(project, member, kind, event_id, trial)
    # Same cell keeps record height/altitude meaningful without inventing terrain edits.
    world.cell_offset(context, target['x'], target['z'])
    contexts, changes = [context], []

    def change(ctx, before, updated):
        if before['raw'] != updated['raw']:
            changes.append({'header': ctx['header']['id'], 'member': ctx['event_member'],
                            'kind': before['kind'], 'id': before['id'], 'offset': before['record_offset'],
                            'before': before['raw'].hex(), 'after': updated['raw'].hex(),
                            'from': public(before), 'to': public(updated)})
    change(context, source, target)
    if kind == 'warp':
        if changes or reciprocal:
            destination, other = endpoint(project, target['destination'], target['destination_warp'], trial)
            require((destination['event_member'], other['record_offset']) != (member, source['record_offset']),
                    'A warp cannot connect to itself', 'EVENT_CONFLICT')
            contexts.append(destination)
            if reciprocal:
                updated = bytearray(other['raw'])
                struct.pack_into('<2H', updated, 4, context['header']['id'], event_id)
                store(project, trial, destination['event_member'], {**other, 'raw': bytes(updated)})
                other_after = lookup(project, destination['event_member'], 'warp', other['id'], trial)
                change(destination, other, other_after)
    if changes:
        if kind == 'npc':
            validate_npc(project, context, target, trial)
        else:
            require(not any(r['kind'] == kind and r['id'] != event_id
                            and (r['x'], r['z']) == (target['x'], target['z'])
                            for r in records(raw_member(project, member, trial))),
                    'Another event of this kind already occupies that tile', 'EVENT_CONFLICT')
    deps = dependencies(project, contexts, index)
    return {'schema': SCHEMA, 'index': index, 'label': label or f'edit {kind} {member}:{event_id}',
            'context': authoring.context_ref(context),
            'request': {'kind': kind, 'event_id': event_id, 'values': copy.deepcopy(values), 'reciprocal': reciprocal},
            'changes': changes, 'dependencies': deps, 'dependencies_sha256': authoring.canonical(deps)}


def replay(project, state, transaction, index):
    try:
        ref = transaction['context']
        ctx = project.context(header=ref['header'], cell=ref['cell'])
        expected = plan(project, ctx, state, index, label=transaction['label'], **transaction['request'])
        require(transaction == expected and bool(expected['changes']),
                'Event before-values, identity, order or dependencies changed', 'BEFORE_VALUE_MISMATCH')
        for change in expected['changes']:
            record = lookup(project, change['member'], change['kind'], change['id'], state)
            store(project, state, change['member'], {**record, 'raw': bytes.fromhex(change['after'])})
            if change['kind'] == 'npc':
                state['event_npcs'][(change['member'], change['id'])] = ctx
    except (KeyError, TypeError, ValueError, struct.error) as exc:
        raise EditorError('INVALID_INPUT', 'Malformed event transaction') from exc


def validate_final(project, state):
    for (member, event_id), ctx in state['event_npcs'].items():
        if ctx['header']['id'] in project._room_headers:
            ctx = project.context(header=ctx['header']['id'], cell=[ctx['cell']['x'], ctx['cell']['y']])
        validate_npc(project, ctx, lookup(project, member, 'npc', event_id, state), state)


def final_patches(project, state):
    patches = []
    for (member, offset), after in sorted(state['event_records'].items()):
        rom_offset, raw = resource(project.blob, world.EVENT_ARCHIVE, member)
        before = raw[offset:offset + len(after)]
        if before != after:
            patches.append({'kind': 'event.record', 'event_member': member, 'rom_offset': rom_offset + offset,
                            'before': before.hex(), 'after': after.hex()})
    return patches


def summary(transaction):
    return {'operation': 'event.transaction', 'index': transaction['index'], 'label': transaction['label'],
            'context': transaction['context'],
            'events': [{k: c[k] for k in ('header', 'member', 'kind', 'id', 'from', 'to')}
                       for c in transaction['changes']]}
