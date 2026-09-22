"""Semantic staged actions, resolved again after each queue edit by Project."""
import copy
import numpy as np
from . import area_layout, scenery, authoring, world, surface_authoring, mapscene, nitro, event_authoring
from .formats import require, EditorError


def operation(kind, ctx, request):
    return {'kind': kind, 'context': ctx, 'request': request}


def cells_to_surfaces(ctx, cells, material, sample=None):
    require(isinstance(cells, list) and 1 <= len(cells) <= 64, 'A stroke needs 1..64 distinct cells')
    coords = set()
    for c in cells:
        require(isinstance(c, dict) and set(c) == {'x', 'z'} and all(type(v) is int for v in c.values()), 'Cell needs integer x/z')
        require((c['x'], c['z']) not in coords, 'Duplicate stroke cell')
        coords.add((c['x'], c['z']))
    # Coalesce horizontal runs, keeping the existing eight-tile writer bound.
    result = []
    while coords:
        x, z = min(coords, key=lambda p: (p[1], p[0])); width = 1
        while width < 8 and (x + width, z) in coords:
            width += 1
        request = dict(x=x, z=z, width=width, height=1, material=material)
        if sample is not None:
            request['sample'] = sample
        result.append(operation('surface', ctx, request))
        coords.difference_update((xx, z) for xx in range(x, x + width))
    return result


def resolve(project, action):
    require(isinstance(action, dict) and set(action) == {'kind', 'context', 'request'}, 'Action needs kind, context and request')
    kind, ref, r = action['kind'], action['context'], action['request']
    require(isinstance(r, dict), 'Action request must be an object')
    ctx = project.context(**ref); state = project.composed()
    if kind in ('interior', 'interaction', 'event', 'scenery', 'group'):
        return [copy.deepcopy(action)]
    if kind == 'paint':
        require({'cells', 'material'}.issubset(r) and set(r).issubset({'cells','material','sample'}), 'Paint needs cells and material')
        return cells_to_surfaces(ref, r['cells'], r['material'], r.get('sample'))
    if kind == 'collision':
        require(set(r) == {'cells', 'blocked'} and type(r['blocked']) is bool, 'Collision needs cells and blocked')
        require(isinstance(r['cells'], list) and 1 <= len(r['cells']) <= 64, 'Choose 1..64 collision cells')
        permissions = []
        for c in r['cells']:
            offset = world.cell_offset(ctx, c['x'], c['z']); raw = project.member_raw(ctx['map_member'])
            pair = state['permissions'].get((ctx['map_member'], offset), raw[offset:offset + 2])
            permissions.append({**c, 'before': pair.hex(), 'after': bytes([pair[0], pair[1] & 127 | (128 if r['blocked'] else 0)]).hex()})
        return [operation('map', ref, {'permissions': permissions})]
    require(kind == 'layout', 'Unknown workflow action')
    require(set(r).issubset({'object_ids', 'dx', 'dz', 'action', 'group', 'cells', 'events', 'repair'}), 'Unknown layout fields')
    name = r.get('group'); group = state.get('groups', {}).get(name) if name else None
    require(not name or group is not None, 'Saved group no longer exists', 'NOT_FOUND')
    require(not group or group['context']['header'] == ctx['header']['id'], 'Group belongs to another map')
    ids = [o['id'] for o in group['objects']] if group else r.get('object_ids', [])
    require(isinstance(ids, list) and ids and len(set(ids)) == len(ids), 'Select distinct objects')
    table = scenery.table_for(project, ctx, state); slots = {o['id']: slot for slot, o in table.items()}
    require(all(i in slots for i in ids), 'Selected object was removed; revise this action', 'NOT_FOUND')
    dx, dz, mode = r.get('dx', 0), r.get('dz', 0), r.get('action', 'move')
    require(mode in ('move', 'duplicate', 'delete'), 'Unsupported layout action')
    cells = group['cells'] if group else r.get('cells', [])
    events = [{k: e[k] for k in ('kind', 'event_id')} for e in group['events']] if group else r.get('events', [])
    repair = group['repair'] if group else r.get('repair')
    require(not group or mode == 'move', 'Saved linked groups can move; remove binding before copy/delete', 'BOUND_OBJECT')
    if mode != 'delete' and cells and (dx or dz or mode == 'duplicate'):
        source = {(c['x'], c['z']) for c in cells}
        linked_npcs = {e['event_id'] for e in events if e['kind'] == 'npc'}
        records = event_authoring.records(event_authoring.raw_member(project, ctx['event_member'], state))
        raw = project.member_raw(ctx['map_member'])
        for x, z in source:
            target = (x + dx, z + dz); offset = world.cell_offset(ctx, *target)
            pair = state['permissions'].get((ctx['map_member'], offset), raw[offset:offset + 2])
            require(mode == 'move' and target in source or not world.is_blocked(pair),
                    'Destination collision overlaps an existing blocker', 'BLOCKED_TILE')
            for e in records:
                if e['kind'] == 'npc' and e['id'] not in linked_npcs:
                    conflict = abs(e['x']-target[0]) <= max(0,e['range_x']) and abs(e['z']-target[1]) <= max(0,e['range_z'])
                elif e['kind'] == 'trigger':
                    conflict = e['x'] <= target[0] < e['x']+e['width'] and e['z'] <= target[1] < e['z']+e['height']
                elif e['kind'] == 'warp' and not any(l['kind']=='warp' and l['event_id']==e['id'] for l in events):
                    conflict = e['x']==target[0] and e['z'] <= target[1] <= e['z']+1
                else:
                    conflict = False
                require(not conflict, 'Destination collision overlaps an actor, entrance or trigger', 'EVENT_CONFLICT')
    ops = area_layout.group(project, ctx, [slots[i] for i in ids], mode, dx, dz, cells=cells, events=events)
    if repair and mode != 'duplicate' and (mode == 'delete' or dx or dz):
        source = {(c['x'], c['z']) for c in repair['cells']}
        destination = {(x + dx, z + dz) for x, z in source} if mode != 'delete' else set()
        exposed = [{'x': x, 'z': z} for x, z in sorted(source - destination)]
        if exposed:
            ops.extend(cells_to_surfaces(ref, exposed, repair['material'], repair.get('sample')))
    if group and (dx or dz):
        moved_cells = [{'x': c['x'] + dx, 'z': c['z'] + dz} for c in cells]
        moved_repair = None if repair is None else {**repair,
                       'cells': [{'x': c['x'] + dx, 'z': c['z'] + dz} for c in repair['cells']]}
        ops.append(operation('group', ref, dict(action='save', name=name, object_ids=ids,
                    cells=moved_cells, events=events, repair=moved_repair)))
    return ops


def sample(project, context, x, z):
    """Pick a qualified repeating floor; reject ambiguous/occluded terrain."""
    world.cell_offset(context, x, z)
    _, blobs = mapscene.tilesets(project, context)
    raw = surface_authoring.model(project, context, project.composed())
    from .surface_native import sample_mapping
    ox, oz = context['origin']
    allowed = {p['material'] for p in surface_authoring.palette(project, context)}
    mapping = sample_mapping(raw, blobs['map_tileset'], x-ox, z-oz)
    require(mapping['material'] in allowed, 'Sample an exposed repeating floor or ground surface', 'UNSUPPORTED_SURFACE')
    return {**mapping, 'x': x, 'z': z}


def plan(project, actions, label=None):
    require(isinstance(actions, list) and 1 <= len(actions) <= 64, 'Choose 1..64 staged actions')
    require(label is None or isinstance(label, str) and 0 < len(label) <= 160, 'Invalid workflow label')
    trial = project; all_transactions = []; summaries = []
    for index, action in enumerate(actions):
        try:
            ops = resolve(trial, action)
            batch = trial.plan_area_edit(ops, label=label)
            trial = trial.area_preview_project(batch)
        except (EditorError, TypeError, KeyError, ValueError) as exc:
            code = exc.code if isinstance(exc, EditorError) else 'INVALID_INPUT'
            raise EditorError(code, f'Action {index + 1}: {exc}') from exc
        all_transactions.extend(batch['transactions'])
        summaries.append({'index': index, 'kind': action['kind'], 'changes': len(batch['transactions'])})
    return {'transactions': all_transactions, 'empty': not all_transactions, 'label': label or 'Map workspace edit',
            'actions': summaries, 'preview': trial.diff()[len(project.diff()):]}
