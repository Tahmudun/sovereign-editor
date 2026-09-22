"""Remember explicit object, collision, event and repair selections in transactions."""
import copy
from . import authoring, scenery, world, event_authoring
from .formats import require, EditorError

SCHEMA = 'sovereign-linked-group-v1'


def capture(project, context, state, name, object_ids, cells=(), events=(), repair=None):
    require(isinstance(name, str) and 0 < len(name.strip()) <= 80, 'Use a group name of 1..80 characters')
    require(isinstance(object_ids, list) and 1 <= len(object_ids) <= 32 and len(set(object_ids)) == len(object_ids),
            'Select distinct objects for this group')
    table = scenery.table_for(project, context, state)
    by_id = {o['id']: o for o in table.values()}
    require(all(i in by_id for i in object_ids), 'A group object is absent', 'NOT_FOUND')
    objects = [{'id': i, **authoring.global_from_record(context, scenery.words(by_id[i]['raw']))} for i in object_ids]
    require(isinstance(cells, (list, tuple)) and len(cells) <= 64, 'Select up to 64 collision cells')
    selected = []
    for c in cells:
        require(isinstance(c, dict) and set(c) == {'x', 'z'}, 'Cell needs x/z')
        offset = world.cell_offset(context, c['x'], c['z'])
        raw = project.member_raw(context['map_member'])
        pair = state['permissions'].get((context['map_member'], offset), raw[offset:offset + 2])
        require(pair[1] & 128, 'Explicit collision selection must be blocked', 'BEFORE_VALUE_MISMATCH')
        require(c not in selected, 'Duplicate collision cell')
        selected.append(copy.deepcopy(c))
    require(isinstance(events, (list, tuple)) and len(events) <= 32, 'Choose at most 32 event links')
    links = []
    for e in events:
        require(isinstance(e, dict) and set(e) == {'kind', 'event_id'}, 'Event link needs kind and event_id')
        r = event_authoring.lookup(project, context['event_member'], e['kind'], e['event_id'], state)
        require(not any((l['kind'], l['event_id']) == (e['kind'], e['event_id']) for l in links), 'Duplicate event link')
        links.append({**e, 'x': r['x'], 'z': r['z']})
    if repair is not None:
        from .surface_authoring import palette
        require(isinstance(repair, dict) and {'cells','material'}.issubset(repair) and set(repair).issubset({'cells','material','sample'}), 'Floor repair needs cells and material')
        if 'sample' in repair:
            require(isinstance(repair['sample'],dict) and set(repair['sample'])=={'x','z'}, 'Sample needs x/z')
            world.cell_offset(context,repair['sample']['x'],repair['sample']['z'])
        require(any(p['material'] == repair['material'] for p in palette(project, context)), 'Choose a compatible repair surface')
        require(isinstance(repair['cells'], list) and 1 <= len(repair['cells']) <= 64, 'Select 1..64 repair cells')
        seen = set()
        for c in repair['cells']:
            require(isinstance(c, dict) and set(c) == {'x', 'z'}, 'Repair cell needs x/z')
            world.cell_offset(context, c['x'], c['z'])
            require((c['x'], c['z']) not in seen, 'Duplicate repair cell')
            seen.add((c['x'], c['z']))
    return {'name': name.strip(), 'context': authoring.context_ref(context), 'objects': objects,
            'cells': selected, 'events': links, 'repair': copy.deepcopy(repair)}


def plan(project, context, state, index, action, name, object_ids=None, cells=(), events=(), repair=None):
    require(action in ('save', 'remove'), 'Unknown group operation')
    groups = state.get('groups', {})
    before = copy.deepcopy(groups.get(name))
    if action == 'remove':
        require(before is not None, 'No saved group with this name', 'NOT_FOUND')
        require(before['context']['header'] == context['header']['id'], 'Group belongs to a different map')
        after = None
    else:
        after = capture(project, context, state, name, object_ids, cells, events, repair)
        require(after['name'] == name, 'Remove leading/trailing spaces from group name')
        require(before is None or before['context'] == after['context'], 'Group name is already used in another map')
        used = {o['id'] for n, g in groups.items() if n != name for o in g['objects']}
        require(not used.intersection(object_ids), 'An object already belongs to another saved group', 'BOUND_OBJECT')
    request = dict(action=action, name=name, object_ids=object_ids, cells=list(cells), events=list(events), repair=repair)
    deps = authoring.dependencies(context, index)
    return {'schema': SCHEMA, 'index': index, 'context': authoring.context_ref(context),
            'label': f'{action.capitalize()} group: {name}', 'request': copy.deepcopy(request),
            'before': before, 'after': after, 'dependencies': deps, 'dependencies_sha256': authoring.canonical(deps)}


def replay(project, state, t, index):
    try:
        ctx = project.context(header=t['context']['header'], cell=t['context']['cell'])
        require(t == plan(project, ctx, state, index, **t['request']), 'Saved group before-values changed', 'BEFORE_VALUE_MISMATCH')
        groups = state.setdefault('groups', {})
        if t['after'] is None:
            del groups[t['request']['name']]
        else:
            groups[t['request']['name']] = copy.deepcopy(t['after'])
    except (KeyError, TypeError, ValueError) as exc:
        raise EditorError('INVALID_INPUT', 'Malformed group transaction') from exc


def validate(project, state):
    for g in state.get('groups', {}).values():
        ctx = project.context(header=g['context']['header'], cell=g['context']['cell'])
        fresh = capture(project, ctx, state, g['name'], [o['id'] for o in g['objects']],
                        g['cells'], [{k: e[k] for k in ('kind', 'event_id')} for e in g['events']], g['repair'])
        require(fresh == g, 'This edit separates a saved group. Move the group or remove its binding first.', 'BOUND_OBJECT')


def summary(t):
    return {'operation': 'group.binding', 'index': t['index'], 'label': t['label'],
            'context': t['context'], 'name': t['request']['name'], 'removed': t['after'] is None}
