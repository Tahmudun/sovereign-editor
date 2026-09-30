"""Reusable map groups: capture, place (copy), move and remove across cells (GROUP-01/02).

A template is an explicit selection in one map cell, stored relative to an anchor tile:
stock objects (buildings, fences, trees: their 48-byte placement records), custom prop
instances (asset + own collision), explicitly selected tiles (behavior + collision bytes),
authored signs/NPCs (kind, text, facing, appearance) and entrance tiles (warps of a
connection). Nothing is duplicated by reference: placing a template runs the ordinary
operations of each element in one area edit (one undo), and a final bookkeeping transaction
records which created objects, instances, tiles, interactions and connection form the group.

Shared vs copied, reported before anything is written:
* shared: stock building models/textures of the area (and custom prop asset packages); a
  revision of a prop asset reaches every copy;
* copied: placement records, collision/behavior bytes, sign/NPC text (new scripts in the
  target map's own banks), NPC local IDs (allocated), prop instances (new keys);
* entrances: a copy never shares an interior (its return warp can lead to only one door): the
  request names a destination and arrival tile and a new two-way connection is created, or the
  door stays unconnected (reported). Moving a group moves both ends' door tile of its own
  connection (world entrance move), so both connection ends stay coherent.
Moves are within one cell (whole tiles); a copy may go to any cell whose area uses the same
building models/textures and whose ground height matches (the element planners refuse otherwise).
Pure planning; Project owns writes.
"""
import copy
import re

from . import authoring, world
from .formats import EditorError, require

SCHEMA = 'sovereign-map-group-v2'
KEY = re.compile(r'[a-z][a-z0-9_]{0,23}')
MAX_ELEMENTS = 32


def templates(state):
    return (state.get('map_groups') or {}).get('templates', {})


def placed(state):
    return (state.get('map_groups') or {}).get('instances', {})


def is_transaction(t):
    return isinstance(t, dict) and t.get('schema') == SCHEMA


def _tile(v, what):
    require(isinstance(v, dict) and set(v) == {'x', 'z'} and all(type(v[k]) is int for k in 'xz'),
            f'{what} needs integer x/z', 'INVALID_INPUT')
    return v['x'], v['z']


def _connection_of(state, header, x, z):
    for c in state.get('world', {}).get('connections', []):
        for w in c['warps']:
            if w['header'] == header and (w['x'], w['z']) == (x, z):
                return c['index']
    return None


def capture(project, context, state, anchor, objects=(), props=(), cells=(), interactions=(), entrances=()):
    from . import scenery, props as prop_mod, simple_interactions as si
    ax, az = _tile(anchor, 'Anchor')
    world.cell_offset(context, ax, az)
    ref = authoring.context_ref(context)
    member = context['map_member']
    total = sum(len(v) for v in (objects, props, cells, interactions, entrances))
    require(1 <= total <= MAX_ELEMENTS, f'Select 1..{MAX_ELEMENTS} elements', 'INVALID_INPUT')
    table = scenery.table_for(project, context, state)
    baseline = {p['slot']: project.member_raw(member)[p['record_offset']:p['record_offset'] + 48]
                for p in project.member_data(member)[1]}
    out = {'objects': [], 'props': [], 'cells': [], 'interactions': [], 'entrances': []}
    for slot in objects:
        require(type(slot) is int and slot in table, f'Object {slot} is not in this cell', 'NOT_FOUND')
        obj = table[slot]
        reason = scenery.protected(obj, member, slot, state)
        require(reason is None, reason or '', 'BOUND_OBJECT')
        raw = obj['raw']
        # A stock record with the same model and parameters lets the object be copied to other maps;
        # without one it can still be duplicated within this map.
        donor = next((s for s, r in sorted(baseline.items()) if r[:4] == raw[:4] and r[16:] == raw[16:]), None)
        pos = authoring.global_from_record(context, scenery.words(raw))
        out['objects'].append({'slot': slot, 'donor': donor, 'raw': raw.hex(), 'dx': pos['x'] - ax, 'dz': pos['z'] - az})
    placed_props = prop_mod.instances(state)
    for key in props:
        inst = placed_props.get(key)
        require(inst is not None and inst['context'] == ref, f'Prop instance {key} is not in this cell', 'NOT_FOUND')
        out['props'].append({'instance': key, 'asset': inst['asset'], 'collision': copy.deepcopy(inst['collision']),
                             'dx': inst['x'] - ax, 'dz': inst['z'] - az})
    for c in cells:
        x, z = _tile(c, 'Cell')
        offset = world.cell_offset(context, x, z)
        pair = state['permissions'].get((member, offset), project.member_raw(member)[offset:offset + 2])
        out['cells'].append({'dx': x - ax, 'dz': z - az, 'pair': bytes(pair).hex()})
    specs = si.specs(state)
    for identity in interactions:
        s = specs.get(identity)
        require(s is not None and s['context'] == ref, f'Interaction {identity} is not in this cell', 'NOT_FOUND')
        out['interactions'].append({'identity': identity, 'kind': s['kind'], 'dialogue': copy.deepcopy(s['dialogue']),
                                    'facing': s['facing'], 'sprite': s.get('sprite'), 'donor_id': s.get('donor_id'),
                                    'dx': s['x'] - ax, 'dz': s['z'] - az})
    for c in entrances:
        x, z = _tile(c, 'Entrance')
        conn = _connection_of(state, context['header']['id'], x, z)
        require(conn is not None, f'Tile {x},{z} is not an authored connection entrance', 'NOT_FOUND')
        out['entrances'].append({'dx': x - ax, 'dz': z - az, 'connection': conn})
    require(len(out['entrances']) <= 1, 'A group carries at most one entrance', 'INVALID_INPUT')
    return {'source': ref, 'anchor': {'x': ax, 'z': az}, **out}


def report(template):
    return {'shared': ['stock building models/textures of the area'] if template['objects'] else [],
            'shared_assets': sorted({p['asset'] for p in template['props']}),
            'copied': {'objects': len(template['objects']), 'prop_instances': len(template['props']),
                       'tiles': len(template['cells']), 'sign_npc_text': len(template['interactions'])},
            'entrance': 'new two-way connection when placed with a destination; otherwise unconnected'
                        if template['entrances'] else None}


def plan(project, context, state, index, action, name=None, label=None, **fields):
    """Template bookkeeping: capture/forget. Place/move/remove expand in Project.plan_area_edit."""
    require(action in ('capture', 'forget', 'record'), 'Unknown map group action', 'INVALID_INPUT')
    require(isinstance(name, str) and KEY.fullmatch(name), 'Group names: lowercase letters/digits/_ (1..24)',
            'INVALID_INPUT')
    require(label is None or isinstance(label, str) and 0 < len(label) <= 160, 'Invalid label', 'INVALID_INPUT')
    if action == 'record':
        return plan_record(project, context, state, index, name, label=label, **fields)
    before = copy.deepcopy(templates(state).get(name))
    if action == 'forget':
        require(before is not None, f'No group template {name}', 'NOT_FOUND')
        require(not fields, 'Forget takes only the name', 'INVALID_INPUT')
        users = sorted(k for k, i in placed(state).items() if i['template'] == name)
        after, rep = None, {'placed_copies_kept': users}
    else:
        require(set(fields) <= {'anchor', 'objects', 'props', 'cells', 'interactions', 'entrances'},
                'Capture fields: anchor, objects, props, cells, interactions, entrances', 'INVALID_INPUT')
        after = capture(project, context, state, **fields)
        after['revision'] = (before or {}).get('revision', 0) + 1
        require(before is None or {k: v for k, v in before.items() if k != 'revision'}
                != {k: v for k, v in after.items() if k != 'revision'}, 'The template is unchanged', 'NO_CHANGE')
        rep = report(after)
    return {'schema': SCHEMA, 'index': index, 'action': action, 'name': name, 'context': authoring.context_ref(context),
            'label': label or f'Group template {action}: {name}',
            'request': {'action': action, 'name': name, 'label': label, **copy.deepcopy(fields)},
            'before': before, 'after': after, 'report': rep}


# ---- expansion ----------------------------------------------------------------------------------

def _prop_key(instance, i):
    return f'{instance[:18]}_{i}'


def _pair(project, context, state, x, z):
    member = context['map_member']
    offset = world.cell_offset(context, x, z)
    return bytes(state['permissions'].get((member, offset), project.member_raw(member)[offset:offset + 2])).hex()


def expand(project, context, state, request):
    """(element operations in order, record fields) of a place/move/remove; each operation is
    one ordinary transaction of its own element type."""
    from . import props as prop_mod, scenery, simple_interactions as si, world_authoring
    action = request.get('action')
    ref = authoring.context_ref(context)
    header, cell = context['header']['id'], [context['cell']['x'], context['cell']['y']]
    ops = []
    if action == 'place':
        require(set(request) <= {'action', 'name', 'instance', 'x', 'z', 'entrance', 'label'},
                'Place fields: name, instance, x, z, entrance', 'INVALID_INPUT')
        t = templates(state).get(request.get('name'))
        require(t is not None, f"No group template {request.get('name')}", 'NOT_FOUND')
        key = request.get('instance')
        require(isinstance(key, str) and KEY.fullmatch(key), 'Instance names: lowercase letters/digits/_', 'INVALID_INPUT')
        require(key not in placed(state), f'Group instance {key} exists', 'EXISTS')
        x, z = _tile({'x': request.get('x'), 'z': request.get('z')}, 'Anchor')
        src = project.context(header=t['source']['header'], cell=t['source']['cell'])
        same_member = src['map_member'] == context['map_member']
        target_slots = {p['slot']: project.member_raw(context['map_member'])[p['record_offset']:p['record_offset'] + 48]
                        for p in project.member_data(context['map_member'])[1]}
        for o in t['objects']:
            raw = bytes.fromhex(o['raw'])
            local = next((s for s, r in sorted(target_slots.items()) if r[:4] == raw[:4] and r[16:] == raw[16:]), None)
            pos = {'x': x + o['dx'], 'z': z + o['dz']}
            table = scenery.table_for(project, src, state)
            if local is not None:
                ops.append(('scenery', ref, {'operation': 'add', 'slot': local, **pos}))
            elif same_member and o['slot'] in table and table[o['slot']]['raw'][:4] == raw[:4] \
                    and table[o['slot']]['raw'][16:] == raw[16:]:
                ops.append(('scenery', ref, {'operation': 'duplicate', 'slot': o['slot'], **pos}))
            else:
                require(o['donor'] is not None and not same_member,
                        'This object has no stock record to copy from outside its own map', 'UNSUPPORTED_EDIT')
                ops.append(('scenery', t['source'], {'operation': 'import', 'slot': o['donor'], **pos,
                                                      'destination': {'header': header, 'cell': cell}}))
        registry = prop_mod.assets(state)
        for i, p in enumerate(t['props']):
            asset = registry.get(p['asset'])
            require(asset is not None, f"Prop asset {p['asset']} is no longer registered", 'NOT_FOUND')
            ops.append(('prop', ref, {'action': 'place', 'asset': p['asset'], 'instance': _prop_key(key, i),
                                       'x': x + p['dx'], 'z': z + p['dz'], 'collision': copy.deepcopy(p['collision']),
                                       'expected_revision': asset['revision']}))
        tiles = [(x + c['dx'], z + c['dz'], c['pair']) for c in t['cells']]
        before = [_pair(project, context, state, tx, tz) for tx, tz, _ in tiles]
        paint = [{'x': tx, 'z': tz, 'before': b, 'after': pair} for (tx, tz, pair), b in zip(tiles, before) if b != pair]
        if paint:
            ops.append(('map', ref, {'permissions': paint}))
        created = world_authoring.created(project, context)
        for s in t['interactions']:
            req = {'action': 'create', 'kind': s['kind'], 'x': x + s['dx'], 'z': z + s['dz'], 'facing': s['facing'],
                   'dialogue': copy.deepcopy(s['dialogue'])}
            if created:
                req.update(donor_id=None, sprite=s['sprite'] if s['kind'] == 'npc' else None)
            else:
                require(same_member, 'Copy signs/NPCs within their own map or into a created area', 'UNSUPPORTED_EDIT')
                req.update(donor_id=s['donor_id'])
            ops.append(('interaction', ref, req))
        entrance = request.get('entrance')
        if entrance is not None:
            require(t['entrances'], 'This template has no entrance tile', 'INVALID_INPUT')
            require(isinstance(entrance, dict) and set(entrance) == {'destination', 'arrival'},
                    'Entrance needs destination and arrival', 'INVALID_INPUT')
            e = t['entrances'][0]
            ops.append(('world', ref, {'action': 'connect', 'x': x + e['dx'], 'z': z + e['dz'],
                                       'destination': entrance['destination'], 'arrival': entrance['arrival']}))
        record = {'verb': 'place', 'instance': key, 'x': x, 'z': z, 'cells': [[tx, tz] for tx, tz, _ in tiles],
                  'pairs': [pair for _, _, pair in tiles], 'cell_before': before}
        return ops, record
    require(action in ('move', 'remove'), 'Unknown map group action', 'INVALID_INPUT')
    inst = placed(state).get(request.get('instance'))
    require(inst is not None, f"No group instance {request.get('instance')}", 'NOT_FOUND')
    require(inst['context'] == ref, 'Choose the group instance map cell', 'CONTEXT_MISMATCH')
    if action == 'move':
        require(set(request) <= {'action', 'instance', 'x', 'z', 'label'}, 'Move fields: instance, x, z', 'INVALID_INPUT')
        x, z = _tile({'x': request.get('x'), 'z': request.get('z')}, 'Anchor')
        dx, dz = x - inst['anchor']['x'], z - inst['anchor']['z']
        require(dx or dz, 'The group is already there', 'NO_CHANGE')
    else:
        require(set(request) <= {'action', 'instance', 'label'}, 'Remove takes the instance', 'INVALID_INPUT')
        require(inst['connection'] is None, 'This copy owns a two-way connection (connections are permanent); move '
                'the group instead', 'BOUND_EVENT')
        x, z, dx, dz = None, None, 0, 0
    table = scenery.table_for(project, context, state)
    for slot in inst['objects']:
        require(slot in table, 'A group object was removed; the group is out of date', 'BOUND_OBJECT')
        pos = authoring.global_from_record(context, scenery.words(table[slot]['raw']))
        ops.append(('scenery', ref, {'operation': 'move', 'slot': slot, 'x': pos['x'] + dx, 'z': pos['z'] + dz}
                    if action == 'move' else {'operation': 'delete', 'slot': slot}))
    for key in inst['props']:
        p = prop_mod.instances(state)[key]
        ops.append(('prop', ref, {'action': 'move', 'instance': key, 'x': p['x'] + dx, 'z': p['z'] + dz}
                    if action == 'move' else {'action': 'remove', 'instance': key}))
    # Tiles the group painted: old tiles get back what was under them; moved tiles are painted
    # with the group's own bytes and remember what they cover (overlapping tiles keep the original).
    old = {tuple(c): (b, pair) for c, b, pair in zip(inst['cells'], inst['cell_before'], inst['pairs'])}
    new = {} if action == 'remove' else {(cx + dx, cz + dz): pair for (cx, cz), (_, pair) in old.items()}
    changes, cell_before = [], []
    for (cx, cz), (b, _) in sorted(old.items()):
        if (cx, cz) not in new:
            now = _pair(project, context, state, cx, cz)
            if now != b:
                changes.append({'x': cx, 'z': cz, 'before': now, 'after': b})
    for (tx, tz), pair in sorted(new.items()):
        now = _pair(project, context, state, tx, tz)
        cell_before.append(old[(tx, tz)][0] if (tx, tz) in old else now)
        if now != pair:
            changes.append({'x': tx, 'z': tz, 'before': now, 'after': pair})
    if changes:
        ops.append(('map', ref, {'permissions': changes}))
    for identity in inst['interactions']:
        s = si.specs(state)[identity]
        ops.append(('interaction', ref, {'action': 'edit', 'identity': identity, 'x': s['x'] + dx, 'z': s['z'] + dz}
                    if action == 'move' else {'action': 'delete', 'identity': identity}))
    if action == 'move' and inst['connection'] is not None:
        conn = next(c for c in state['world']['connections'] if c['index'] == inst['connection'])
        mine = [w for w in conn['warps'] if w['header'] == header]
        ops.append(('world', ref, {'action': 'move', 'connection': inst['connection'],
                                   'source': [{'x': w['x'] + dx, 'z': w['z'] + dz} for w in mine]}))
    record = {'verb': action, 'instance': request['instance'], 'x': x, 'z': z,
              'cells': [list(c) for c in sorted(new)], 'pairs': [new[c] for c in sorted(new)], 'cell_before': cell_before}
    return ops, record


def plan_record(project, context, state, index, name, instance=None, verb=None, produced=(), x=None, z=None,
                cells=(), pairs=(), cell_before=(), label=None):
    """Bookkeeping after a place/move/remove: which elements form the instance."""
    from . import props as prop_mod, scenery, simple_interactions as si, world_authoring
    require(verb in ('place', 'move', 'remove'), 'Unknown record verb', 'INVALID_INPUT')
    edits = project.doc['map_edits']
    require(all(type(i) is int and 0 <= i < index for i in produced), 'Group record names earlier transactions',
            'BEFORE_VALUE_MISMATCH')
    ts = [edits[i] for i in produced]
    before = copy.deepcopy(placed(state).get(instance))
    ref = authoring.context_ref(context)
    shape = {'cells': [list(c) for c in cells], 'pairs': list(pairs), 'cell_before': list(cell_before)}
    require(len(shape['cells']) == len(shape['pairs']) == len(shape['cell_before']), 'Group tiles differ',
            'INVALID_INPUT')
    if verb == 'remove':
        require(before is not None, f'No group instance {instance}', 'NOT_FOUND')
        after = None
    elif verb == 'move':
        require(before is not None, f'No group instance {instance}', 'NOT_FOUND')
        after = {**before, 'anchor': {'x': x, 'z': z}, **shape}
    else:
        require(before is None, f'Group instance {instance} exists', 'EXISTS')
        t = templates(state).get(name)
        require(t is not None, f'No group template {name}', 'NOT_FOUND')
        objects, props, interactions, connection = [], [], [], None
        for tr in ts:
            if tr.get('schema') == scenery.SCHEMA:
                objects += [c['slot'] for c in tr['objects'] if c['after'] is not None and c['before'] is None]
            elif tr.get('schema') == prop_mod.SCHEMA and tr['action'] == 'place':
                props.append(tr['effect']['instance'])
            elif tr.get('schema') in si.SCHEMAS and tr.get('after') is not None:
                interactions.append(tr['identity'])
            elif tr.get('schema') == world_authoring.WARP_SCHEMA:
                connection = tr['index']
        require(len(objects) == len(t['objects']) and len(props) == len(t['props'])
                and len(interactions) == len(t['interactions']), 'The group copy is incomplete', 'BEFORE_VALUE_MISMATCH')
        after = {'template': name, 'revision': t['revision'], 'context': ref, 'anchor': {'x': x, 'z': z},
                 'objects': objects, 'props': props, 'interactions': interactions, 'connection': connection, **shape}
    return {'schema': SCHEMA, 'index': index, 'action': 'record', 'name': name, 'context': ref,
            'label': label or f'Group {verb}: {instance}',
            'request': {'action': 'record', 'name': name, 'instance': instance, 'verb': verb, 'produced': list(produced),
                        'x': x, 'z': z, **shape, 'label': label},
            'before': before, 'after': after, 'report': None}


def replay(project, state, t, index):
    try:
        ctx = project.context(header=t['context']['header'], cell=t['context']['cell'])
        expected = plan(project, ctx, state, index, **t['request'])
    except (KeyError, TypeError) as exc:
        raise EditorError('INVALID_INPUT', 'Malformed map group transaction') from exc
    require(expected == t, 'Map group before-values or members differ', 'BEFORE_VALUE_MISMATCH')
    root = state.setdefault('map_groups', {})
    if t['action'] == 'record':
        table = root.setdefault('instances', {})
        if t['after'] is None:
            table.pop(t['request']['instance'], None)
        else:
            table[t['request']['instance']] = copy.deepcopy(t['after'])
    elif t['after'] is None:
        root.setdefault('templates', {}).pop(t['name'], None)
    else:
        root.setdefault('templates', {})[t['name']] = copy.deepcopy(t['after'])


def summary(t):
    return {'operation': 'map.group', 'index': t['index'], 'action': t['action'], 'name': t['name'],
            'label': t['label'], 'report': t['report'],
            'instance': t['request'].get('instance'), 'removed': t['after'] is None}


def view(state):
    return {'templates': {k: {**v, 'report': report(v)} for k, v in sorted(templates(state).items())},
            'instances': dict(sorted(placed(state).items()))}
