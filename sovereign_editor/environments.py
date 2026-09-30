"""Reusable named environments (KIT-02): a kit of ground materials, prop assets and placeable presets.

An environment names what an author reuses to build one kind of place — an outdoor town
(lawn/dirt/cobble paving, raised streets, groves, beds, fence runs, petal clearings), a coast
(a stock donor cell with sand, shore, rocks and native water) or a cave (room shapes with
exits) — and is revised, retired and reused across projects like the packages it depends on.

Presets are placed with ``{'action': 'place', 'environment', 'preset', 'x', 'z', 'key'}``
and expand, before planning, into the ordinary operations an author could write by hand
(elevation terrace/pond/cave room, border paving, prop placements, ground decals, or an area
created from the donor cell). History therefore replays without this module's placement
code; one placement is one area edit (one undo). Terrain art stays separate from height,
traversal, encounter and collision rules: each expanded operation keeps its own rules.

Preset types (offsets are tiles from the placement anchor):
* ``terrace``   width, height, access [{side, offset}], optional ``pave`` material for the top
* ``pond``      width, height, traversable
* ``cave_room`` rects [[dx, dz, w, h]], exits [{dx, dz}], encounters
* ``props``     items [{asset, dx, dz, collision}] (or ``from_group``: a prop-only group template)
* ``decals``    items [{material, decal, dx, dz, rotation, flip, layer}] (or ``from_decals``:
                {header, cell, keys, anchor} copies placed decals)
* ``area``      donor (a role in ``donors``: a stock cell) and encounters: a new area from it

Environment transactions are project-wide; Project owns writes; UI and CLI share the
area-edit path. Environments add nothing to an export by themselves.
"""
import copy
import re

from .formats import EditorError, require

SCHEMA = 'sovereign-environment-transaction-v1'
ACTIONS = ('define', 'revise', 'retire')
KEY = re.compile(r'[a-z][a-z0-9_]{0,23}')
PRESET_KEY = re.compile(r'[a-z][a-z0-9_]{0,23}')
PLACE_KEY = re.compile(r'[a-z][a-z0-9_]{0,7}')
KINDS = ('outdoor', 'coast', 'cave')
TYPES = ('terrace', 'pond', 'cave_room', 'props', 'decals', 'area')
FIELDS = ('display', 'kind', 'ground', 'props', 'presets', 'donors', 'notes')
MAX_PRESETS, MAX_ITEMS, MAX_ENVIRONMENTS = 32, 64, 64


def is_transaction(t):
    return isinstance(t, dict) and t.get('schema') == SCHEMA


def catalog(state):
    return state.get('environments') or {}


def _int(v, lo, hi, what):
    require(type(v) is int and lo <= v <= hi, f'{what} is an integer {lo}..{hi}', 'INVALID_INPUT')
    return v


def _offset(v, what, step):
    require(isinstance(v, (int, float)) and not isinstance(v, bool) and -64 <= v <= 64
            and float(v / step).is_integer(), f'{what} is a tile offset in steps of {step}', 'INVALID_INPUT')
    return float(v)


def _preset(project, state, env, name, spec):
    """Validated, resolved copy of one preset (``from_group`` / ``from_decals`` become items)."""
    from . import ground_materials as gm, map_groups, terrain_authoring as ta
    require(isinstance(name, str) and PRESET_KEY.fullmatch(name), 'Preset names: lowercase letters/digits/_ (<= 24)',
            'INVALID_INPUT')
    require(isinstance(spec, dict) and spec.get('type') in TYPES, f"Preset {name}: type is one of {', '.join(TYPES)}",
            'INVALID_INPUT')
    kind = spec['type']
    allowed = {'terrace': {'width', 'height', 'access', 'pave'}, 'pond': {'width', 'height', 'traversable'},
               'cave_room': {'rects', 'exits', 'encounters'}, 'props': {'items', 'from_group'},
               'decals': {'items', 'from_decals'}, 'area': {'donor', 'encounters'}}[kind]
    require(set(spec) - {'type', 'label'} <= allowed, f"Preset {name}: {kind} fields are {', '.join(sorted(allowed))}",
            'INVALID_INPUT')
    out = {'type': kind, **({'label': spec['label']} if spec.get('label') else {})}
    if kind == 'terrace':
        out['width'] = _int(spec.get('width'), *ta.TOP_SIDE, f'Preset {name}: width')
        out['height'] = _int(spec.get('height'), *ta.TOP_SIDE, f'Preset {name}: height')
        access = spec.get('access', [])
        require(isinstance(access, list) and len(access) <= ta.MAX_ACCESS
                and all(isinstance(a, dict) and set(a) == {'side', 'offset'} for a in access),
                f'Preset {name}: access lists up to {ta.MAX_ACCESS} stairs {{side, offset}}', 'INVALID_INPUT')
        # The terrace family checks sides and offsets; run it on a trial anchor now.
        ta.normalise({'action': 'terrace', 'x': 0, 'z': 0, 'width': out['width'], 'height': out['height'],
                      'access': access})
        out['access'] = copy.deepcopy(access)
        pave = spec.get('pave')
        require(pave is None or pave in env['ground'], f'Preset {name}: pave with one of the environment ground materials',
                'NOT_FOUND')
        out['pave'] = pave
    elif kind == 'pond':
        out['width'] = _int(spec.get('width'), *ta.POND_SIDE, f'Preset {name}: width')
        out['height'] = _int(spec.get('height'), *ta.POND_SIDE, f'Preset {name}: height')
        require(type(spec.get('traversable', True)) is bool, f'Preset {name}: traversable is true/false', 'INVALID_INPUT')
        out['traversable'] = spec.get('traversable', True)
    elif kind == 'cave_room':
        rects = spec.get('rects')
        require(isinstance(rects, list) and 1 <= len(rects) <= ta.SHAPE_RECTS
                and all(isinstance(r, list) and len(r) == 4 and all(type(v) is int for v in r) for r in rects),
                f'Preset {name}: rects lists 1..{ta.SHAPE_RECTS} [dx, dz, w, h]', 'INVALID_INPUT')
        exits = spec.get('exits', [])
        require(isinstance(exits, list) and len(exits) <= ta.CAVE_EXITS
                and all(isinstance(e, dict) and set(e) == {'dx', 'dz'} and all(type(v) is int for v in e.values())
                        for e in exits), f'Preset {name}: exits lists up to {ta.CAVE_EXITS} {{dx, dz}}', 'INVALID_INPUT')
        require(type(spec.get('encounters', False)) is bool, f'Preset {name}: encounters is true/false', 'INVALID_INPUT')
        out.update(rects=copy.deepcopy(rects), exits=copy.deepcopy(exits), encounters=spec.get('encounters', False))
    elif kind == 'props':
        items = spec.get('items')
        if spec.get('from_group') is not None:
            require(items is None, f'Preset {name}: items or from_group', 'INVALID_INPUT')
            template = map_groups.templates(state).get(spec['from_group'])
            require(template is not None, f"No group template {spec['from_group']}", 'NOT_FOUND')
            require(template['props'] and not any(template[k] for k in ('objects', 'cells', 'interactions', 'entrances')),
                    f"Group {spec['from_group']} holds more than prop instances; only prop-only groups travel with an "
                    'environment', 'UNSUPPORTED_GROUP')
            items = [{'asset': p['asset'], 'dx': p['dx'], 'dz': p['dz'], 'collision': p['collision']}
                     for p in template['props']]
        require(isinstance(items, list) and 1 <= len(items) <= MAX_ITEMS, f'Preset {name}: 1..{MAX_ITEMS} props',
                'INVALID_INPUT')
        clean = []
        for item in items:
            require(isinstance(item, dict) and set(item) == {'asset', 'dx', 'dz', 'collision'},
                    f'Preset {name}: a prop item is {{asset, dx, dz, collision}}', 'INVALID_INPUT')
            require(item['asset'] in env['props'], f"Preset {name}: asset {item['asset']} is not in the environment",
                    'NOT_FOUND')
            dx, dz = _offset(item['dx'], 'dx', 0.5), _offset(item['dz'], 'dz', 0.5)
            require(dx % 1 == 0.5 and dz % 1 == 0.5, f'Preset {name}: props stand on tile centres (offsets n + 0.5)',
                    'INVALID_INPUT')
            require(isinstance(item['collision'], list) and all(
                isinstance(c, list) and len(c) == 2 and all(type(v) is int for v in c) for c in item['collision']),
                f'Preset {name}: collision is a list of [dx, dz]', 'INVALID_INPUT')
            clean.append({'asset': item['asset'], 'dx': dx, 'dz': dz, 'collision': copy.deepcopy(item['collision'])})
        out['items'] = clean
    elif kind == 'decals':
        items = spec.get('items')
        if spec.get('from_decals') is not None:
            require(items is None, f'Preset {name}: items or from_decals', 'INVALID_INPUT')
            src = spec['from_decals']
            require(isinstance(src, dict) and set(src) == {'header', 'cell', 'keys', 'anchor'},
                    f'Preset {name}: from_decals is {{header, cell, keys, anchor}}', 'INVALID_INPUT')
            ctx = project.context(header=src['header'], cell=src['cell'])
            placed = (state.get('ground_decals') or {}).get(ctx['map_member'], {})
            require(isinstance(src['keys'], list) and src['keys'] and all(k in placed for k in src['keys']),
                    f'Preset {name}: every key must be a decal in that cell', 'NOT_FOUND')
            ax, az = src['anchor']['x'], src['anchor']['z']
            items = [{**{k: v for k, v in placed[k].items() if k not in ('x', 'z')},
                      'dx': placed[k]['x'] - ax, 'dz': placed[k]['z'] - az} for k in src['keys']]
        require(isinstance(items, list) and 1 <= len(items) <= MAX_ITEMS, f'Preset {name}: 1..{MAX_ITEMS} decals',
                'INVALID_INPUT')
        clean = []
        for item in items:
            require(isinstance(item, dict) and {'material', 'decal', 'dx', 'dz'} <= set(item)
                    <= {'material', 'decal', 'dx', 'dz', 'rotation', 'flip', 'layer'},
                    f'Preset {name}: a decal item is {{material, decal, dx, dz[, rotation, flip, layer]}}', 'INVALID_INPUT')
            require(item['material'] in env['ground'], f"Preset {name}: material {item['material']} is not in the "
                    'environment', 'NOT_FOUND')
            entry = gm.materials(state)[item['material']]
            roles = gm.verify_package(project, item['material'], entry['package'])['roles']
            require(f"decal:{item['decal']}" in roles, f"Preset {name}: {item['material']} has no decal {item['decal']}",
                    'NOT_FOUND')
            clean.append({'material': item['material'], 'decal': item['decal'],
                          'dx': _offset(item['dx'], 'dx', 1 / 16), 'dz': _offset(item['dz'], 'dz', 1 / 16),
                          'rotation': item.get('rotation', 0), 'flip': item.get('flip', False), 'layer': item.get('layer', 0)})
        out['items'] = clean
    else:
        require(spec.get('donor') in env['donors'], f'Preset {name}: donor is one of the environment donors',
                'NOT_FOUND')
        require(spec.get('encounters', 'none') in ('none', 'template'), f'Preset {name}: encounters none or template',
                'INVALID_INPUT')
        out.update(donor=spec['donor'], encounters=spec.get('encounters', 'none'))
    return out


def _normalise(project, state, fields):
    from . import ground_materials as gm, props, world
    require(isinstance(fields.get('display'), str) and 0 < len(fields['display']) <= 64, 'An environment needs a display '
            'name (<= 64)', 'INVALID_INPUT')
    require(fields.get('kind') in KINDS, f"Environment kind is one of {', '.join(KINDS)}", 'INVALID_INPUT')
    ground, assets = fields.get('ground', []), fields.get('props', [])
    require(isinstance(ground, list) and len(set(ground)) == len(ground) and all(m in gm.materials(state) for m in ground),
            'ground lists registered ground materials', 'NOT_FOUND')
    require(isinstance(assets, list) and len(set(assets)) == len(assets) and all(a in props.assets(state) for a in assets),
            'props lists registered prop assets', 'NOT_FOUND')
    donors = fields.get('donors', {})
    require(isinstance(donors, dict) and len(donors) <= 8, 'donors maps up to 8 roles to stock cells', 'INVALID_INPUT')
    clean_donors = {}
    for role, cell in sorted(donors.items()):
        require(isinstance(role, str) and PRESET_KEY.fullmatch(role) and isinstance(cell, dict)
                and set(cell) == {'header', 'cell'}, 'A donor is role: {header, cell}', 'INVALID_INPUT')
        require(type(cell['header']) is int and cell['header'] < world.header_count(project.blob),
                f'Donor {role}: a stock header', 'UNSUPPORTED_CONTEXT')
        project.context(header=cell['header'], cell=cell['cell'])
        clean_donors[role] = {'header': cell['header'], 'cell': list(cell['cell'])}
    env = {'display': fields['display'], 'kind': fields['kind'], 'ground': list(ground), 'props': list(assets),
           'donors': clean_donors, 'notes': fields.get('notes')}
    require(env['notes'] is None or isinstance(env['notes'], str) and len(env['notes']) <= 400, 'notes <= 400 characters',
            'INVALID_INPUT')
    presets = fields.get('presets', {})
    require(isinstance(presets, dict) and 1 <= len(presets) <= MAX_PRESETS, f'An environment has 1..{MAX_PRESETS} presets',
            'INVALID_INPUT')
    env['presets'] = {name: _preset(project, state, env, name, spec) for name, spec in sorted(presets.items())}
    return env


def plan(project, state, index, action, key=None, label=None, expected_revision=None, **fields):
    require(action in ACTIONS, f"Environment action is one of {', '.join(ACTIONS)} (placement: action place)",
            'INVALID_INPUT')
    require(isinstance(key, str) and KEY.fullmatch(key), 'An environment needs a key (lowercase, <= 24)', 'INVALID_INPUT')
    require(label is None or isinstance(label, str) and 0 < len(label) <= 160, 'Invalid label', 'INVALID_INPUT')
    require(set(fields) <= set(FIELDS), f"Environment fields are {', '.join(FIELDS)}", 'INVALID_INPUT')
    current = catalog(state)
    before = copy.deepcopy(current.get(key))
    if action == 'define':
        require(before is None, f'Environment {key} already exists; revise it', 'EXISTS')
        require(expected_revision is None, 'A new environment has no revision yet', 'INVALID_INPUT')
        require(len(current) < MAX_ENVIRONMENTS, f'At most {MAX_ENVIRONMENTS} environments', 'RESOURCE_CAPACITY')
        after = {**_normalise(project, state, fields), 'revision': 1, 'retired': False}
    else:
        require(before is not None, f'No environment {key}', 'NOT_FOUND')
        require(not before['retired'], f'Environment {key} is retired', 'RETIRED')
        require(expected_revision == before['revision'],
                f"Environment {key} is at revision {before['revision']}; refresh before editing", 'STALE_ASSET')
        if action == 'retire':
            require(not fields, 'Retire takes only the key and expected_revision', 'INVALID_INPUT')
            after = {**before, 'retired': True}
        else:
            require(fields, 'Name the fields to change', 'INVALID_INPUT')
            merged = {k: copy.deepcopy(before.get(k)) for k in FIELDS}
            merged.update(copy.deepcopy(fields))
            after = {**_normalise(project, state, merged), 'revision': before['revision'] + 1, 'retired': False}
            require({k: v for k, v in after.items() if k != 'revision'} != {k: v for k, v in before.items() if k != 'revision'},
                    'The environment is unchanged', 'NO_CHANGE')
    return {'schema': SCHEMA, 'index': index, 'action': action, 'key': key,
            'label': label or f'{action.capitalize()} environment {key}',
            'request': {'action': action, 'key': key, 'label': label,
                        **({'expected_revision': expected_revision} if expected_revision is not None else {}),
                        **copy.deepcopy(fields)},
            'before': before, 'after': after}


def replay(project, state, t, index):
    try:
        expected = plan(project, state, index, **t['request'])
    except (KeyError, TypeError) as exc:
        raise EditorError('INVALID_INPUT', 'Malformed environment transaction') from exc
    require(expected == t, 'Environment before-value or dependency differs', 'BEFORE_VALUE_MISMATCH')
    state.setdefault('environments', {})[t['key']] = copy.deepcopy(t['after'])


def summary(t):
    return {'operation': 'environment.transaction', 'index': t['index'], 'action': t['action'], 'key': t['key'],
            'label': t['label'], 'revision': t['after']['revision'], 'retired': t['after']['retired']}


def view(project, state):
    from . import ground_materials as gm, props
    out = {}
    for key, env in sorted(catalog(state).items()):
        out[key] = {**copy.deepcopy(env),
                    'dependencies': {'ground': {m: gm.materials(state)[m]['package'] for m in env['ground']
                                                if m in gm.materials(state)},
                                     'props': {a: props.assets(state)[a]['package'] for a in env['props']
                                               if a in props.assets(state)}},
                    'presets': {n: {'type': p['type'], **({'items': len(p['items'])} if 'items' in p else {})}
                                for n, p in env['presets'].items()}}
    return {'environments': out, 'types': list(TYPES), 'kinds': list(KINDS)}


# ---- placement ----------------------------------------------------------------------------

def _cell(context, x, z):
    return {'header': context['header'], 'cell': [int(x) // 32, int(z) // 32]}


def expand(project, context, request):
    """A preset placement as the ordinary operations it stands for (planned by their own rules)."""
    from . import props
    require(isinstance(request, dict) and set(request) - {'label'} <= {'action', 'environment', 'preset', 'x', 'z', 'key',
                                                                       'identity', 'name', 'internal_name', 'worldmap'},
            'Placement fields: environment, preset, x, z, key (and identity, name, internal_name, worldmap for an area)',
            'INVALID_INPUT')
    state = project.composed()
    env = catalog(state).get(request.get('environment'))
    require(env is not None, f"No environment {request.get('environment')}", 'NOT_FOUND')
    require(not env['retired'], f"Environment {request['environment']} is retired", 'RETIRED')
    preset = env['presets'].get(request.get('preset'))
    require(preset is not None, f"Environment {request['environment']} has no preset {request.get('preset')}", 'NOT_FOUND')
    label = request.get('label') or f"{env['display']}: {request['preset']}"
    kind = preset['type']
    if kind == 'area':
        donor = env['donors'][preset['donor']]
        area = {'action': 'create', 'template_header': donor['header'], 'encounters': preset['encounters'],
                'cells': [{'cell': [0, 0], 'source': copy.deepcopy(donor)}], 'close': True, 'label': label,
                'worldmap': request.get('worldmap')}
        for k in ('identity', 'name', 'internal_name'):
            require(isinstance(request.get(k), str), f'An area placement names its {k}', 'INVALID_INPUT')
            area[k] = request[k]
        return [{'kind': 'world', 'context': copy.deepcopy(donor), 'request': area}]
    x, z = request.get('x'), request.get('z')
    require(type(x) is int and type(z) is int and x >= 0 and z >= 0, 'x and z are the anchor tile (integers)',
            'INVALID_INPUT')
    ctx = {'header': context['header'], 'cell': list(context['cell'])}
    if kind == 'terrace':
        ops = [{'kind': 'elevation', 'context': ctx, 'request': {
            'action': 'terrace', 'x': x, 'z': z, 'width': preset['width'], 'height': preset['height'],
            'access': copy.deepcopy(preset['access']), 'label': label}}]
        if preset['pave']:
            tiles = [{'x': x + dx, 'z': z + dz} for dz in range(preset['height']) for dx in range(preset['width'])]
            ops.append({'kind': 'border', 'context': _cell(ctx, x, z), 'request': {
                'family': 'path', 'material': preset['pave'], 'tiles': tiles, 'label': f'{label} paving'}})
        return ops
    if kind == 'pond':
        return [{'kind': 'elevation', 'context': ctx, 'request': {
            'action': 'pond', 'x': x, 'z': z, 'width': preset['width'], 'height': preset['height'],
            'traversable': preset['traversable'], 'label': label}}]
    if kind == 'cave_room':
        return [{'kind': 'elevation', 'context': ctx, 'request': {
            'action': 'cave_room', 'rects': [[x + r[0], z + r[1], r[2], r[3]] for r in preset['rects']],
            'exits': [{'x': x + e['dx'], 'z': z + e['dz']} for e in preset['exits']],
            'encounters': preset['encounters'], 'label': label}}]
    key = request.get('key')
    require(isinstance(key, str) and PLACE_KEY.fullmatch(key), 'Props and decals need a key (lowercase, <= 8) naming '
            'the placed copies', 'INVALID_INPUT')
    if kind == 'props':
        registry = props.assets(state)
        return [{'kind': 'prop', 'context': _cell(ctx, x + item['dx'], z + item['dz']), 'request': {
            'action': 'place', 'asset': item['asset'], 'instance': f'{key}{i}', 'x': x + item['dx'], 'z': z + item['dz'],
            'collision': copy.deepcopy(item['collision']), 'expected_revision': registry[item['asset']]['revision']}}
            for i, item in enumerate(preset['items'])]
    return [{'kind': 'decal', 'context': _cell(ctx, x + item['dx'], z + item['dz']), 'request': {
        'action': 'place', 'key': f'{key}_{i}', 'material': item['material'], 'decal': item['decal'],
        'x': x + item['dx'], 'z': z + item['dz'], 'rotation': item['rotation'], 'flip': item['flip'],
        'layer': item['layer'], 'label': label}} for i, item in enumerate(preset['items'])]
