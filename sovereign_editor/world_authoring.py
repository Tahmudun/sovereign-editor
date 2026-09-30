"""Created areas: new map headers, private resources and explicit connections.

A created area copies visual templates (map members from composed donor cells,
the template header's area data, music and flags) into NEW resources: a header
after the stock table, a private matrix, map members, and fresh empty events,
local scripts, map-load scripts, text and, optionally, a copy of the template's
encounter table. No donor event, script, trigger, flag or text is inherited.

Header records reach the game through the resident extension in world_runtime.
IDs are allocated from the composed resource graph, so interior copies and
earlier created areas never collide. Project owns every write; replay rebuilds
the same bytes and refuses any changed dependency.
"""
import copy
import re
import struct

from . import authoring, world, world_runtime, interiors, dialogue_format as fmt
from .formats import EditorError, baseline_digest, digest, member_count, require

SCHEMA = 'sovereign-world-area-v1'
WARP_SCHEMA = 'sovereign-world-warp-v1'
# v2 (PROD-VIS-001): up to 4x4 cells, donors judged by texture names against the
# template tileset (so stock filler/forest cells qualify), stock altitudes kept.
SCHEMA_V2 = 'sovereign-world-area-v2'
# v3 (PROD-VIS-002): the area texture animation binds tracks to material slots by index, so
# a donor whose slots do not match the template area's track order would animate the wrong
# materials (stock forest filler 208 copied into a new member scrolls like water). v3 refuses
# such donors unless the cell is declared static; static cells are excluded like stock 208.
SCHEMA_V3 = 'sovereign-world-area-v3'
AREA_SCHEMAS = (SCHEMA, SCHEMA_V2, SCHEMA_V3)
# Identity and area-animation transactions (world integration v1) change created headers and
# resident runtime data after creation; world_identity owns them.
from .world_identity import IDENTITY_SCHEMA, ANIMATION_SCHEMA
# Moving a connection's endpoint tiles in place (editor v1, CHAPTER-02): warp IDs stay, so both
# ends keep pointing at each other; new tiles are qualified entrances.
MOVE_SCHEMA = 'sovereign-world-warp-move-v1'
SCHEMAS = (SCHEMA, WARP_SCHEMA, SCHEMA_V2, SCHEMA_V3, IDENTITY_SCHEMA, ANIMATION_SCHEMA, MOVE_SCHEMA)
MAX_CELLS_V1 = 4
WILD = 'a/0/3/7'
NO_ENCOUNTERS = 255
NAME = re.compile(r'[A-Z][A-Z0-9_]{0,14}')
IDENTITY = re.compile(r'[a-z][a-z0-9_-]{0,31}')
EMPTY_EVENTS = struct.pack('<4I', 0, 0, 0, 0)
EMPTY_SCRIPTS = b'\x13\xfd\x00\x00'
EMPTY_INIT = b'\x00\x00\x00\x00'
MAX_CELLS = 16      # PROD-CAP-001: up to a 4x4 rectangle (v1 areas used at most 4 cells)
MAX_SIDE = 4
# Stock warp tiles (evidence/world-authoring-v1): behavior -> (label, blocked, escape step).
WARP_TILES = {0x69: ('door', True, (0, 1)), 0x65: ('exit mat', False, (0, -1)),
              0x6E: ('gate opening', False, (0, 1)),
              # TILE_BEHAVIOR_WARP_SOUTH (pret enum): the stock cave exit in front of a wall hole
              # (D41R0104 (5,8) 6f0a, hole 6580 behind it); step north to leave it.
              0x6F: ('cave exit', False, (0, -1))}
EVENT_LIMIT = 0x800


def reset(project):
    project._world_headers = {}
    project._world_members = {}
    cache = getattr(project, '_event_cache', None)
    if cache:
        stock = member_count(project.blob, world.EVENT_ARCHIVE)
        for member in [m for m in cache if m >= stock]:
            del cache[member]


def created(project, context):
    """A context in a project-created area (no stock events to borrow from)."""
    return context['header']['id'] >= world.header_count(project.blob)


def areas(state):
    return state.get('world', {}).get('areas', {})


def _invalidate(project):
    project._context_cache = {}
    for key in ('_area_resource_users', '_library_source_cache', '_event_users', '_header_names',
                '_gameplay_wild_users'):
        project.__dict__.pop(key, None)


def _next(project, archive, taken):
    if archive == world.MAP_ARCHIVE:
        base = member_count(project.blob, archive) + len(project._room_members)
    elif archive == world.MATRIX_ARCHIVE:
        base = member_count(project.blob, archive) + len(project._room_matrices)
    else:
        base = member_count(project.blob, archive) + sum(a == archive for a, _ in project._world_members)
    value = base + taken.count(archive)
    taken.append(archive)
    return value


def _cell_ref(ref):
    require(isinstance(ref, dict) and set(ref) == {'cell', 'source'}, 'Each cell needs cell and source', 'INVALID_INPUT')
    cell, source = ref['cell'], ref['source']
    require(isinstance(cell, list) and len(cell) == 2 and all(type(v) is int and 0 <= v < MAX_SIDE for v in cell),
            f'Area cells use 0-based matrix coordinates up to {MAX_SIDE - 1}', 'INVALID_INPUT')
    require(isinstance(source, dict) and set(source) <= {'header', 'cell'} and 'header' in source,
            'Each cell needs a donor header and cell', 'INVALID_INPUT')
    return cell, source


def close_edges(raw, context, edges):
    """Block the outer boundary tiles of a created cell; types are preserved."""
    data = bytearray(raw)
    start = context['sections']['permissions_offset']
    closed = []
    for z in range(world.MAP_SIZE):
        for x in range(world.MAP_SIZE):
            if not ((x == 0 and 'west' in edges) or (x == 31 and 'east' in edges)
                    or (z == 0 and 'north' in edges) or (z == 31 and 'south' in edges)):
                continue
            at = start + 2 * (z * world.MAP_SIZE + x)
            if not data[at + 1] & 0x80:
                data[at + 1] |= 0x80
                closed.append([x, z])
    return bytes(data), closed


def matrix_bytes(width, height, name, maps, altitudes=None):
    """Matrix member; an altitude section only when a donor cell has one (v2)."""
    label = name.lower().encode('ascii')
    heights = bool(altitudes) and any(altitudes.values())
    raw = bytes((width, height, 0, 1 if heights else 0, len(label))) + label
    if heights:
        raw += bytes(altitudes[(x, y)] for y in range(height) for x in range(width))
    raw += b''.join(struct.pack('<H', maps[(x, y)]) for y in range(height) for x in range(width))
    world.decode_matrix(raw, -1)
    return raw


def compatible(project, ctx, template):
    """A donor cell renders with the created area's tilesets: same area type and
    every terrain and placed-model texture/palette name present (v2)."""
    from . import nitro
    from .formats import map_data, resource
    area = world.read_area_data(project.blob, template['area_data'])
    require(ctx['area_data']['area_type'] == area['area_type'],
            f"Donor {ctx['id']} is {ctx['area_data']['area_type_name']}; the template is {area['area_type_name']}",
            'INCOMPATIBLE_ASSETS')
    cache = project.__dict__.setdefault('_tileset_names', {})

    def names(archive, member):
        if (archive, member) not in cache:
            textures, palettes = nitro.texture_set(resource(project.blob, archive, member)[1])
            cache[(archive, member)] = (set(textures), set(palettes))
        return cache[(archive, member)]
    missing = set()
    raw = project.member_raw(ctx['map_member'])
    _, props, model = map_data(raw)
    textures, palettes = names(world.MAP_TEXTURE_ARCHIVE, area['map_tileset'])
    for prim in nitro.decode_model(model, geometry_only=True)[1]:
        m = prim.material
        missing |= {n for n, have in ((m['texture_name'], textures), (m['palette_name'], palettes)) if n and n not in have}
    if props:
        archive = world.INTERIOR_MODEL_ARCHIVE if area['area_type'] == 0 else world.BUILDING_MODEL_ARCHIVE
        textures, palettes = names(world.BUILDING_TEXTURE_ARCHIVE, area['buildings_tileset'])
        for model_id in sorted({prop['model_id'] for prop in props}):
            try:
                building = resource(project.blob, archive, model_id)[1]
                parts = nitro.decode_model(building, geometry_only=True)[1]
            except EditorError:
                missing.add(f'building model {model_id}')
                continue
            if b'TEX0' in nitro.blocks(building):
                continue                  # the model carries its own textures
            for prim in parts:
                m = prim.material
                missing |= {n for n, have in ((m['texture_name'], textures), (m['palette_name'], palettes))
                            if n and n not in have}
    require(not missing, f"Donor {ctx['id']} uses textures missing from template tilesets: {sorted(missing)[:5]}",
            'INCOMPATIBLE_ASSETS')


def build(project, context, state, index, identity, name, internal_name, template_header, cells,
          encounters='template', worldmap=None, close=True, label=None, static=None, _version=3):
    """Plan bytes and transaction for one created area; no writes.

    ``static`` (v3) is 'auto' or a list of [x, y] cells excluded from the area texture
    animation; any other cell whose material slots mismatch the animation is refused."""
    require(_version >= 3 or static is None, 'Static cells need area schema v3', 'INVALID_INPUT')
    require(isinstance(identity, str) and IDENTITY.fullmatch(identity),
            'Use a stable identity: lowercase letters, numbers, hyphens or underscores', 'INVALID_INPUT')
    require(identity not in areas(state), f'Area {identity} already exists', 'DUPLICATE_IDENTITY')
    require(isinstance(name, str) and 0 < len(name.strip()) <= 40, 'Area name needs 1..40 characters', 'INVALID_INPUT')
    require(isinstance(internal_name, str) and NAME.fullmatch(internal_name),
            'Internal name needs 1..15 capital letters, digits or underscores', 'INVALID_INPUT')
    require(label is None or isinstance(label, str) and 0 < len(label) <= 160, 'Invalid area label', 'INVALID_INPUT')
    require(type(close) is bool, 'close must be true or false', 'INVALID_INPUT')
    require(internal_name not in {a['internal_name'] for a in areas(state).values()}
            and internal_name not in _stock_names(project), 'Internal map name is already used', 'DUPLICATE_IDENTITY')
    require(encounters in ('template', 'none'), 'Encounters are template or none', 'INVALID_INPUT')
    require(worldmap is None or isinstance(worldmap, list) and len(worldmap) == 2
            and all(type(v) is int and 0 <= v < 64 for v in worldmap), 'World-map position is two values 0..63', 'INVALID_INPUT')
    require(type(template_header) is int, 'Choose a template header', 'INVALID_INPUT')
    template = project.header(template_header)
    limit = MAX_CELLS if _version >= 2 else MAX_CELLS_V1
    require(isinstance(cells, list) and 1 <= len(cells) <= limit, f'An area has 1..{limit} cells', 'INVALID_INPUT')
    parsed = [_cell_ref(c) for c in cells]
    grid = {tuple(c): s for c, s in parsed}
    require(len(grid) == len(parsed), 'Area cells must be distinct', 'INVALID_INPUT')
    width, height = max(c[0] for c, _ in parsed) + 1, max(c[1] for c, _ in parsed) + 1
    require(width * height == len(grid), 'Area cells must fill their rectangle', 'INVALID_INPUT')
    first = project.context(**parsed[0][1])
    require(authoring.context_ref(context) == authoring.context_ref(first),
            'Open the first donor cell to create this area', 'CONTEXT_MISMATCH')
    count = len(project._world_headers)
    require(count < world_runtime.MAX_HEADERS, f'At most {world_runtime.MAX_HEADERS} created map headers are supported',
            'RESOURCE_CAPACITY')
    header_id = world.header_count(project.blob) + count
    wild = None
    if encounters == 'template':
        require(template['wild_pokemon'] != NO_ENCOUNTERS and template['wild_pokemon'] < member_count(project.blob, WILD),
                'The template has no encounter table to copy', 'NO_ENCOUNTERS')
    taken = []
    tracks = None
    if _version >= 3:
        from . import world_identity
        tracks = world_identity.animation_tracks(project, template['area_data'])
        require(static is None or static == 'auto' or isinstance(static, list) and all(
            isinstance(c, list) and len(c) == 2 and tuple(c) in grid for c in static),
            'static is "auto" or a list of [x, y] cells of this area', 'INVALID_INPUT')
        require(tracks or not isinstance(static, list) or not static,
                'The template area has no texture animation; no cell needs to be static', 'INVALID_INPUT')
    donors, maps, created_cells = {}, {}, []
    for (x, y), source in sorted(grid.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        ctx = project.context(**source)
        if _version >= 2:
            compatible(project, ctx, template)
        else:
            require(ctx['area_data']['id'] == template['area_data'],
                    'Donor cells must use the template header’s area data and textures', 'INCOMPATIBLE_ASSETS')
        donors[(x, y)] = ctx
        maps[(x, y)] = _next(project, world.MAP_ARCHIVE, taken)
    allocation = {'header': header_id, 'matrix': _next(project, world.MATRIX_ARCHIVE, taken),
                  'maps': [maps[k] for k in sorted(maps, key=lambda k: (k[1], k[0]))],
                  'events': _next(project, world.EVENT_ARCHIVE, taken),
                  'scripts': _next(project, fmt.SCRIPT_ARCHIVE, taken),
                  'level_script': _next(project, fmt.SCRIPT_ARCHIVE, taken),
                  'text': _next(project, fmt.TEXT_ARCHIVE, taken),
                  'wild': _next(project, WILD, taken) if encounters == 'template' else None}
    require(allocation['wild'] is None or allocation['wild'] < NO_ENCOUNTERS,
            'Encounter tables are exhausted (u8 header field)', 'RESOURCE_CAPACITY')
    require(max(v for k, v in allocation.items() if k not in ('maps', 'wild') and v is not None) < 65535
            and max(allocation['maps']) < 65535, 'Resource capacity exceeded', 'RESOURCE_CAPACITY')
    payload = {'maps': {}, 'members': {}}
    for (x, y), ctx in sorted(donors.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        raw = interiors.map_bytes(project, ctx, state)
        edges = set()
        if close:
            edges = {e for e, outer in (('west', x == 0), ('east', x == width - 1),
                                        ('north', y == 0), ('south', y == height - 1)) if outer}
        data, closed = close_edges(raw, ctx, edges)
        payload['maps'][maps[(x, y)]] = data
        created_cells.append({'cell': [x, y], 'map_member': maps[(x, y)], 'source': authoring.context_ref(ctx),
                              'source_sha256': digest(raw), 'sha256': digest(data), 'closed': closed})
        if _version >= 2:
            created_cells[-1]['altitude'] = ctx['cell']['altitude'] or 0
        if _version >= 3:
            # Tracks bind by slot index to whichever map block the viewing header loads, so a
            # slot is safe only when its material is the track's own material (stock design).
            from . import world_identity
            mismatch = world_identity.slot_mismatch(project, ctx['map_member'], tracks) if tracks else []
            chosen = bool(mismatch) if static == 'auto' else [x, y] in (static or [])
            require(not mismatch or chosen,
                    f'Donor cell {x},{y} (header {ctx["header"]["id"]}) has material slots the area texture '
                    f'animation would move: {[(m["slot"], m["material"], m["track"]) for m in mismatch][:3]}; '
                    'declare the cell static', 'INCOMPATIBLE_ANIMATION')
            created_cells[-1]['static'] = chosen
            created_cells[-1]['mismatched_slots'] = len(mismatch)
    altitudes = {tuple(c['cell']): c['altitude'] for c in created_cells} if _version >= 2 else None
    matrix = matrix_bytes(width, height, internal_name, maps, altitudes)
    head = copy.deepcopy(template)
    head.update(matrix=allocation['matrix'], event_file=allocation['events'], script_file=allocation['scripts'],
                level_script=allocation['level_script'], text_archive=allocation['text'],
                wild_pokemon=allocation['wild'] if allocation['wild'] is not None else NO_ENCOUNTERS)
    if worldmap is not None:
        head['worldmap'] = {**head['worldmap'], 'x': worldmap[0], 'y': worldmap[1]}
    record = world.encode_header(head)
    key = fmt.text_entries(project.resource(fmt.TEXT_ARCHIVE, template['text_archive'])[1])[0]
    members = {(world.EVENT_ARCHIVE, allocation['events']): EMPTY_EVENTS,
               (fmt.SCRIPT_ARCHIVE, allocation['scripts']): EMPTY_SCRIPTS,
               (fmt.SCRIPT_ARCHIVE, allocation['level_script']): EMPTY_INIT,
               (fmt.TEXT_ARCHIVE, allocation['text']): struct.pack('<HH', 0, key)}
    wild_source = None
    if allocation['wild'] is not None:
        from . import gameplay
        wild_source = gameplay.current(project, state, WILD, template['wild_pokemon'])
        members[(WILD, allocation['wild'])] = wild_source
    payload['members'] = members
    payload['matrix'] = matrix
    payload['header'] = {'raw': record, 'name': internal_name}
    deps = {'baseline': baseline_digest(project), 'template': {'header': template_header, 'hex': template['hex']},
            'sources': [{'context': authoring.dependencies(donors[tuple(c['cell'])], index),
                         'map_sha256': c['source_sha256']} for c in created_cells],
            'wild_sha256': digest(wild_source) if wild_source is not None else None, 'text_key': key}
    request = dict(action='create', identity=identity, name=name, internal_name=internal_name,
                   template_header=template_header, cells=copy.deepcopy(cells), encounters=encounters,
                   worldmap=copy.deepcopy(worldmap), close=close, label=label)
    if _version >= 3:
        deps['animation_tracks'] = tracks
        request['static'] = copy.deepcopy(static)
    schema = SCHEMA_V3 if _version >= 3 else SCHEMA_V2 if _version >= 2 else SCHEMA
    transaction = {'schema': schema, 'index': index, 'identity': identity,
                   'label': label or f'Create area: {name}',
                   'context': authoring.context_ref(first), 'request': request, 'allocation': allocation,
                   'header': {'id': header_id, 'hex': record.hex(), 'internal_name': internal_name, 'name': name},
                   'size': [width, height], 'cells': created_cells, 'matrix_sha256': digest(matrix),
                   'resources': {f'{a}:{m}': digest(v) for (a, m), v in sorted(members.items())},
                   'dependencies': deps, 'dependencies_sha256': authoring.canonical(deps)}
    return transaction, payload


# ---- template review --------------------------------------------------------------
OFFSETS = {(-1, 0): 'W', (1, 0): 'E', (0, -1): 'N', (0, 1): 'S',
           (-1, -1): 'NW', (1, -1): 'NE', (-1, 1): 'SW', (1, 1): 'SE'}


def overhangs(project, member):
    """Materials a map model draws outside its own cell, by neighbor offset."""
    from . import nitro
    from .formats import map_data
    cache = project.__dict__.setdefault('_overhang_cache', {})
    if member not in cache:
        result = {}
        for prim in nitro.decode_model(map_data(project.member_raw(member))[2], geometry_only=True)[1]:
            for tri in prim.triangles:
                v = prim.vertices[tri]
                dx = -1 if v[:, 0].min() < -256.5 else 1 if v[:, 0].max() > 256.5 else 0
                dz = -1 if v[:, 2].min() < -256.5 else 1 if v[:, 2].max() > 256.5 else 0
                for d in {(dx, 0), (0, dz), (dx, dz)} - {(0, 0)}:
                    if d[0] and not dx or d[1] and not dz:
                        continue
                    result.setdefault(d, set()).add(prim.material['name'])
        cache[member] = {k: sorted(v) for k, v in result.items()}
    return cache[member]


def review(project, request):
    """Cross-cell dependency review of a create request, before apply (read-only).

    ``internal_missing``: a stock neighbor draws into a cell (canopies, overhanging
    ground) but the layout puts a different donor there. ``foreign_overhangs``: a
    layout neighbor that is not the stock one draws into the cell. ``boundary_losses``:
    stock neighbors outside the area that drew into its outer cells; these matter only
    if a reachable camera view can see them.
    """
    require(isinstance(request, dict) and isinstance(request.get('cells'), list), 'Review needs a create request',
            'INVALID_INPUT')
    layout, stock_of = {}, {}
    for ref in request['cells']:
        cell, source = _cell_ref(ref)
        ctx = project.context(**source)
        layout[tuple(cell)] = ctx
        stock_of[tuple(cell)] = (ctx['matrix']['id'], ctx['cell']['x'], ctx['cell']['y'])
    grids = {}

    def stock_neighbor(identity, d):
        matrix, x, y = identity
        grid = grids.setdefault(matrix, project.matrix_data(matrix))
        nx, ny = x + d[0], y + d[1]
        if not (0 <= nx < grid['width'] and 0 <= ny < grid['height']) or grid['maps'][ny][nx] == world.EMPTY:
            return None
        return (matrix, nx, ny), grid['maps'][ny][nx]
    internal, foreign, boundary = [], [], []
    for pos, ctx in sorted(layout.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        for d, name in OFFSETS.items():
            back = (-d[0], -d[1])
            neighbor = stock_neighbor(stock_of[pos], d)
            placed = (pos[0] + d[0], pos[1] + d[1])
            if neighbor is not None:
                incoming = overhangs(project, neighbor[1]).get(back)
                if incoming:
                    row = {'position': list(pos), 'side': name, 'stock_member': neighbor[1], 'materials': incoming}
                    if placed not in layout:
                        boundary.append(row)
                    elif stock_of[placed] != neighbor[0]:
                        internal.append({**row, 'from': list(placed)})
            if placed in layout and (neighbor is None or stock_of[placed] != neighbor[0]):
                cast = overhangs(project, layout[placed]['map_member']).get(back)
                if cast:
                    foreign.append({'position': list(pos), 'side': name, 'from': list(placed),
                                    'member': layout[placed]['map_member'], 'materials': cast})
    return {'cells': len(layout), 'internal_missing': internal, 'foreign_overhangs': foreign,
            'boundary_losses': boundary,
            'altitudes': {f'{x},{y}': c['cell']['altitude'] or 0 for (x, y), c in sorted(layout.items())},
            'scope': 'geometry drawn outside each cell; textures, collision and camera views are reviewed separately'}


def _stock_names(project):
    cache = getattr(project, '_stock_header_names', None)
    if cache is None:
        _, table = world.span_named(project.blob, world.NAME_TABLE)
        cache = project._stock_header_names = {
            table[i:i + world.NAME_LENGTH].split(b'\0')[0].decode('ascii', 'replace')
            for i in range(0, len(table), world.NAME_LENGTH)}
    return cache


def plan(project, context, state, index, action, **request):
    require(action in ('create', 'connect', 'identity', 'animation', 'move'), 'Unknown world action', 'INVALID_INPUT')
    if action == 'move':
        return plan_move(project, context, state, index, **request)
    if action in ('identity', 'animation'):
        from . import world_identity
        return world_identity.plan(project, context, state, index, action, **request)
    if action == 'connect':
        return plan_connect(project, context, state, index, **request)
    return build(project, context, state, index, **request)[0]


def register(project, state, transaction, payload):
    area = transaction['allocation']
    project._world_headers[area['header']] = {'raw': payload['header']['raw'], 'name': payload['header']['name'],
                                              'identity': transaction['identity']}
    project._room_matrices[area['matrix']] = payload['matrix']
    for member, data in payload['maps'].items():
        project._room_members[member] = data
    project._world_members.update(payload['members'])
    _invalidate(project)
    state.setdefault('world', {}).setdefault('areas', {})[transaction['identity']] = {
        'identity': transaction['identity'], 'name': transaction['header']['name'],
        'internal_name': transaction['header']['internal_name'], 'header': area['header'],
        'allocation': copy.deepcopy(area), 'size': transaction['size'], 'index': transaction['index'],
        'template_header': transaction['request']['template_header'],
        'cells': [{'cell': c['cell'], 'map_member': c['map_member'], 'source': c['source'], 'closed': len(c['closed'])}
                  for c in transaction['cells']]}
    static = sorted(c['map_member'] for c in transaction['cells'] if c.get('static'))
    if static:
        state['world'].setdefault('static', {})[transaction['identity']] = static
    for c in transaction['cells']:
        state['contexts'].append(project.context(header=area['header'], cell=c['cell']))


def replay(project, state, transaction, index):
    if transaction.get('schema') in (IDENTITY_SCHEMA, ANIMATION_SCHEMA):
        from . import world_identity
        return world_identity.replay(project, state, transaction, index)
    try:
        if transaction['schema'] == WARP_SCHEMA:
            return replay_connect(project, state, transaction, index)
        if transaction['schema'] == MOVE_SCHEMA:
            return replay_move(project, state, transaction, index)
        ref = transaction['context']
        ctx = project.context(header=ref['header'], cell=ref['cell'])
        request = {k: v for k, v in transaction['request'].items() if k != 'action'}
        version = {SCHEMA: 1, SCHEMA_V2: 2, SCHEMA_V3: 3}[transaction['schema']]
        expected, payload = build(project, ctx, state, index, **request, _version=version)
        require(transaction == expected, 'Created area dependencies or allocation differ', 'BEFORE_VALUE_MISMATCH')
        register(project, state, transaction, payload)
    except (KeyError, TypeError, ValueError, struct.error) as exc:
        raise EditorError('INVALID_INPUT', 'Malformed world transaction') from exc


# ---- connections ------------------------------------------------------------

def warps(state, member):
    return state.get('world_warps', {}).get(member, [])


def append_warps(project, member, state, raw):
    added = warps(state, member)
    if not added:
        return raw
    cursor, result = 0, bytearray()
    for kind, size in (('background', 20), ('npc', 32), ('warp', 12), ('trigger', 16)):
        count = struct.unpack_from('<I', raw, cursor)[0]
        cursor += 4
        extra = [bytes.fromhex(w['raw']) for w in added] if kind == 'warp' else []
        result.extend(struct.pack('<I', count + len(extra)))
        result.extend(raw[cursor:cursor + count * size])
        result.extend(b''.join(extra))
        cursor += count * size
    require(cursor == len(raw), 'Unexpected event bytes')
    return bytes(result)


def tile(project, state, header, x, z):
    from . import event_authoring as ev
    require(type(x) is int and type(z) is int, 'Warp tiles are integer coordinates', 'INVALID_INPUT')
    ctx = ev.location(project, header, x, z)
    offset = world.cell_offset(ctx, x, z)
    pair = state['permissions'].get((ctx['map_member'], offset), project.member_raw(ctx['map_member'])[offset:offset + 2])
    return ctx, pair


def check_endpoint(project, state, header, x, z, rows, exclude=()):
    """A warp tile needs a qualified stock warp behavior and a walkable escape."""
    ctx, pair = tile(project, state, header, x, z)
    require(pair[0] in WARP_TILES, f'Tile {x},{z} is not a door, exit mat or gate opening (behavior {pair[0]:#04x})',
            'UNSUPPORTED_ENTRANCE')
    label, blocked, (dx, dz) = WARP_TILES[pair[0]]
    require(bool(pair[1] & 0x80) == blocked, f'The {label} at {x},{z} has unexpected collision', 'UNSUPPORTED_ENTRANCE')
    try:
        _, escape = tile(project, state, header, x + dx, z + dz)
    except EditorError as exc:
        raise EditorError('UNSUPPORTED_ENTRANCE', f'The {label} at {x},{z} has no arrival tile') from exc
    require(not world.is_blocked(escape), f'The arrival step from {x},{z} is blocked', 'BLOCKED_TILE')
    for r in rows:
        if (r['kind'], r['id']) in exclude:
            continue
        if r['kind'] == 'warp':
            conflict = (r['x'], r['z']) == (x, z)
        elif r['kind'] == 'npc':
            conflict = any(abs(r['x'] - px) <= max(0, r['range_x']) and abs(r['z'] - pz) <= max(0, r['range_z'])
                           for px, pz in ((x, z), (x + dx, z + dz)))
        elif r['kind'] == 'trigger':
            conflict = any(r['x'] <= px < r['x'] + r['width'] and r['z'] <= pz < r['z'] + r['height']
                           for px, pz in ((x, z), (x + dx, z + dz)))
        else:
            conflict = False
        require(not conflict, f'The {label} at {x},{z} overlaps an existing warp, actor or trigger', 'EVENT_CONFLICT')
    return ctx, label


def plan_connect(project, context, state, index, x, z, destination, arrival, extra=(), arrival_extra=(), label=None):
    from . import event_authoring as ev
    require(isinstance(destination, dict) and set(destination) == {'header'} and type(destination['header']) is int,
            'Destination needs a header', 'INVALID_INPUT')
    require(isinstance(arrival, dict) and set(arrival) == {'x', 'z'}, 'Arrival needs x/z', 'INVALID_INPUT')
    require(label is None or isinstance(label, str) and 0 < len(label) <= 160, 'Invalid connection label', 'INVALID_INPUT')
    source_header = context['header']['id']
    target_header = destination['header']
    target = project.header(target_header)
    same = target_header == source_header
    source_member, target_member = context['event_member'], target['event_file']
    require(same or source_member != target_member, 'Both areas share one event file', 'SHARED_RESOURCE')
    world.cell_offset(context, x, z)
    source_rows = ev.records(ev.raw_member(project, source_member, state))
    target_rows = ev.records(ev.raw_member(project, target_member, state))
    require(isinstance(extra, (list, tuple)) and len(extra) <= 3
            and all(isinstance(e, dict) and set(e) == {'x', 'z'} for e in extra),
            'A wide entrance adds at most three extra x/z tiles', 'INVALID_INPUT')
    tiles = [(x, z)] + [(e['x'], e['z']) for e in extra]
    require(len(set(tiles)) == len(tiles), 'Entrance tiles must be distinct', 'INVALID_INPUT')
    source_ctx, source_kind = check_endpoint(project, state, source_header, x, z, source_rows)
    for tx, tz in tiles[1:]:
        world.cell_offset(context, tx, tz)
        require(abs(tx - x) + abs(tz - z) <= 3 and check_endpoint(project, state, source_header, tx, tz, source_rows)[1] == source_kind,
                'Extra entrance tiles must be nearby tiles of the same entrance kind', 'UNSUPPORTED_ENTRANCE')
    require(isinstance(arrival_extra, (list, tuple)) and len(arrival_extra) <= 3
            and all(isinstance(e, dict) and set(e) == {'x', 'z'} for e in arrival_extra),
            'A wide arrival adds at most three extra x/z tiles', 'INVALID_INPUT')
    landing = [(arrival['x'], arrival['z'])] + [(e['x'], e['z']) for e in arrival_extra]
    require(len(set(landing)) == len(landing), 'Arrival tiles must be distinct', 'INVALID_INPUT')
    target_ctx, target_kind = check_endpoint(project, state, target_header, arrival['x'], arrival['z'], target_rows)
    for tx, tz in landing[1:]:
        require(abs(tx - arrival['x']) + abs(tz - arrival['z']) <= 3
                and check_endpoint(project, state, target_header, tx, tz, target_rows)[1] == target_kind,
                'Extra arrival tiles must be nearby tiles of the same entrance kind', 'UNSUPPORTED_ENTRANCE')
    source_id = sum(r['kind'] == 'warp' for r in source_rows)
    # Same area (a cave hole to another part of the cave): both ends go into one event file,
    # the arrival warps directly after the entrance warps; they must be different tiles.
    require(not same or not set(tiles) & set(landing), 'An entrance cannot lead to itself', 'INVALID_DESTINATION')
    target_id = source_id + len(tiles) if same else sum(r['kind'] == 'warp' for r in target_rows)
    forward = [struct.pack('<4HI', tx, tz, target_header, target_id, 0) for tx, tz in tiles]
    backward = [struct.pack('<4HI', tx, tz, source_header, source_id, 0) for tx, tz in landing]
    for member, added in ((((source_member, len(forward) + len(backward)),) if same else
                           ((source_member, len(forward)), (target_member, len(backward))))):
        size = len(ev.raw_member(project, member, state)) + 12 * added
        require(size <= EVENT_LIMIT, 'Event file exceeds the HGSS 0x800-byte buffer', 'RESOURCE_CAPACITY')
    changes = [{'header': source_header, 'member': source_member, 'id': source_id + i, 'x': tx, 'z': tz,
                'destination': target_header, 'destination_warp': target_id, 'kind': source_kind, 'raw': record.hex()}
               for i, ((tx, tz), record) in enumerate(zip(tiles, forward))]
    changes.extend({'header': target_header, 'member': target_member, 'id': target_id + i, 'x': tx, 'z': tz,
                    'destination': source_header, 'destination_warp': source_id, 'kind': target_kind, 'raw': record.hex()}
                   for i, ((tx, tz), record) in enumerate(zip(landing, backward)))
    deps = {'source': authoring.dependencies(source_ctx, index), 'target': authoring.dependencies(target_ctx, index),
            'source_events': digest(ev.raw_member(project, source_member, state)),
            'target_events': digest(ev.raw_member(project, target_member, state)),
            'target_header': target['hex']}
    request = dict(action='connect', x=x, z=z, destination=copy.deepcopy(destination),
                   arrival=copy.deepcopy(arrival), extra=[dict(e) for e in extra],
                   arrival_extra=[dict(e) for e in arrival_extra], label=label)
    return {'schema': WARP_SCHEMA, 'index': index, 'context': authoring.context_ref(context),
            'label': label or f'Connect {context["name"]} to {target["name"]}', 'request': request,
            'warps': changes, 'dependencies': deps, 'dependencies_sha256': authoring.canonical(deps)}


def plan_move(project, context, state, index, connection, source=None, target=None, label=None):
    """Move one or both ends of an existing connection to new qualified entrance tiles."""
    from . import event_authoring as ev
    conns = {c['index']: c for c in state.get('world', {}).get('connections', [])}
    require(type(connection) is int and connection in conns, 'Choose an existing connection (its index)', 'NOT_FOUND')
    require(label is None or isinstance(label, str) and 0 < len(label) <= 160, 'Invalid label', 'INVALID_INPUT')
    conn = conns[connection]
    first = conn['warps'][0]
    sides = {'source': [w for w in conn['warps'] if w['header'] == first['header'] and w['destination'] == first['destination']
                        and w['member'] == first['member'] and w['destination_warp'] == first['destination_warp']]}
    sides['target'] = [w for w in conn['warps'] if w not in sides['source']]
    require(source is not None or target is not None, 'Move the source tiles, the target tiles or both', 'INVALID_INPUT')
    moves, deps = [], {'connection': authoring.canonical(conn['warps'])}
    for side, tiles in (('source', source), ('target', target)):
        if tiles is None:
            continue
        old = sides[side]
        require(isinstance(tiles, list) and len(tiles) == len(old)
                and all(isinstance(t, dict) and set(t) == {'x', 'z'} and all(type(t[k]) is int for k in t) for t in tiles),
                f'The {side} side has {len(old)} tile(s); give that many {{x, z}}', 'INVALID_INPUT')
        header, member = old[0]['header'], old[0]['member']
        rows = ev.records(ev.raw_member(project, member, state))
        exclude = {('warp', w['id']) for w in old}
        kinds = set()
        for t in tiles:
            ctx, kind = check_endpoint(project, state, header, t['x'], t['z'], rows, exclude=exclude)
            kinds.add(kind)
        require(len({(t['x'], t['z']) for t in tiles}) == len(tiles), 'New tiles must be distinct', 'INVALID_INPUT')
        if len(tiles) > 1:
            require(all(abs(t['x'] - tiles[0]['x']) + abs(t['z'] - tiles[0]['z']) <= 3 for t in tiles) and len(kinds) == 1,
                    'A wide entrance keeps nearby tiles of one entrance kind', 'UNSUPPORTED_ENTRANCE')
        for w, t in zip(old, tiles):
            raw = struct.pack('<4HI', t['x'], t['z'], w['destination'], w['destination_warp'], 0)
            moves.append({'member': member, 'header': header, 'id': w['id'],
                          'before': {'x': w['x'], 'z': w['z'], 'raw': w['raw']},
                          'after': {'x': t['x'], 'z': t['z'], 'raw': raw.hex(), 'kind': next(iter(kinds))}})
        deps[f'{side}_events'] = digest(ev.raw_member(project, member, state))
    require(any(m['before']['raw'] != m['after']['raw'] for m in moves), 'The entrance is already there', 'NO_CHANGE')
    deps['context'] = authoring.context_ref(context)
    request = dict(action='move', connection=connection, source=copy.deepcopy(source), target=copy.deepcopy(target),
                   label=label)
    return {'schema': MOVE_SCHEMA, 'index': index, 'context': authoring.context_ref(context),
            'label': label or f"Move entrance of {conn['label']}", 'request': request, 'connection': connection,
            'moves': moves, 'dependencies': deps, 'dependencies_sha256': authoring.canonical(deps)}


def replay_move(project, state, transaction, index):
    ref = transaction['context']
    ctx = project.context(header=ref['header'], cell=ref['cell'])
    request = {k: v for k, v in transaction['request'].items() if k != 'action'}
    expected = plan_move(project, ctx, state, index, **request)
    require(transaction == expected, 'Entrance move before-values or dependencies differ', 'BEFORE_VALUE_MISMATCH')
    table = state.setdefault('world_warps', {})
    conn = next(c for c in state['world']['connections'] if c['index'] == transaction['connection'])
    # Replace (never mutate): composed warp rows may be the connection transaction's own dicts.
    for m in transaction['moves']:
        for rows in (table.get(m['member'], []), conn['warps']):
            for i, w in enumerate(rows):
                if w['member'] == m['member'] and w['id'] == m['id']:
                    rows[i] = {**w, 'x': m['after']['x'], 'z': m['after']['z'], 'raw': m['after']['raw'],
                               'kind': m['after']['kind']}
    state['contexts'].append(ctx)


def replay_connect(project, state, transaction, index):
    ref = transaction['context']
    ctx = project.context(header=ref['header'], cell=ref['cell'])
    request = {k: v for k, v in transaction['request'].items() if k != 'action'}
    expected = plan_connect(project, ctx, state, index, **request)
    require(transaction == expected, 'Connection before-values or dependencies differ', 'BEFORE_VALUE_MISMATCH')
    table = state.setdefault('world_warps', {})
    for change in transaction['warps']:
        table.setdefault(change['member'], []).append(change)
    state.setdefault('world', {}).setdefault('connections', []).append(
        {'index': index, 'label': transaction['label'], 'warps': copy.deepcopy(transaction['warps'])})
    state['contexts'].append(ctx)


def validate(project, state):
    """Final composed state: connections still land on qualified, clear tiles."""
    from . import event_authoring as ev
    for connection in state.get('world', {}).get('connections', []):
        for change in connection['warps']:
            rows = ev.records(ev.raw_member(project, change['member'], state))
            here = [r for r in rows if r['kind'] == 'warp' and r['id'] == change['id']]
            require(len(here) == 1 and here[0]['raw'].hex() == change['raw'],
                    'A connection warp was changed or removed', 'BEFORE_VALUE_MISMATCH')
            check_endpoint(project, state, change['header'], change['x'], change['z'], rows,
                           exclude={('warp', change['id'])})
            project.header(change['destination'])
    from . import story_authoring, world_identity
    world_identity.validate(project, state)
    from . import petal_effect
    from . import pokemon_packages
    if (project._world_headers or story_authoring.catalog(state, 'character') or story_authoring.catalog(state, 'trainer')
            or petal_effect.areas(state) or pokemon_packages.identities(state)):
        # Resident capacity is refused here, before any write, not at export.
        runtime_plan(project, state)


def created_headers(project, state):
    """(record, stock template) per created header, in ID order."""
    by_header = {a['header']: a for a in areas(state).values()}
    stock = world.header_count(project.blob)
    result = []
    for h in sorted(project._world_headers):
        template = by_header[h]['template_header']
        while template >= stock:
            template = by_header[template]['template_header']
        result.append((project._world_headers[h]['raw'], template))
    return result


def runtime_plan(project, state):
    """All runtime additions. Historical bounds keep resident layout v1 byte for
    byte; beyond them one resident.Layout (v2) places every overlay-129 item."""
    from . import story_authoring, resident, world_identity
    headers = created_headers(project, state)
    counts = (len(story_authoring.catalog(state, 'character')), len(story_authoring.catalog(state, 'trainer')), len(headers))
    # World-integration additions (identity overrides, static members, town-map locations)
    # are resident items, so they always use layout v2 allocation; older states are unchanged.
    extras = world_identity.extras_present(state) or bool(world_identity.identities(state))
    from . import travel_points
    from . import tutor_labels
    from . import petal_effect
    extras = (extras or bool(travel_points.points(state)) or bool(tutor_labels.tutors(state))
              or bool(petal_effect.areas(state)))
    from . import pokemon_packages
    extras = extras or bool(pokemon_packages.identities(state))
    headers = petal_effect.header_records(project, state, headers)
    layout = resident.Layout(project.blob) if resident.needed(*counts) or extras else None
    plan = story_authoring.runtime(project, state, layout=layout)
    from . import game_data
    plan = game_data.runtime(project, state, plan, layout)
    from . import field_services
    plan = field_services.bindings(project.blob, plan, state)
    if headers:
        plan = world_runtime.bindings(project.blob, plan, headers if layout else [r for r, _ in headers], layout=layout)
    if layout is not None:
        plan = world_runtime.static_bindings(project.blob, plan, world_identity.static_list(state), layout)
        plan = world_runtime.town_bindings(project.blob, plan, world_identity.town_specs(project, state), layout)
        if travel_points.points(state):
            ids = story_authoring.allocation(project, state)
            scripts = {p['spawn']: ids['travel:' + k][0] for k, p in travel_points.points(state).items()
                       if 'travel:' + k in ids}
            plan = travel_points.bindings(project, state, plan, layout, scripts)
        from . import tutor_labels
        plan = tutor_labels.bindings(project.blob, plan, state, layout)
        if (state.get('world') or {}).get('connections'):
            # R82-WARP-01: authored connections may join openings that face the same way.
            from . import warp_arrival
            plan = warp_arrival.bindings(project.blob, plan, layout)
        # EFFECT-01: airborne petals (weather 14) in the chosen areas.
        plan = petal_effect.bindings(project, state, plan, layout)
        # POKE-01..04: custom Pokémon form identities (records, icon palettes, follower tags).
        plan = pokemon_packages.bindings(project, state, plan, layout)
        plan = resident.finish(project.blob, plan, layout)
    # R101-SHOP: applied base-ROM repairs (archive appends only).
    from . import runtime_repairs
    plan = runtime_repairs.bindings(project, state, plan)
    return plan


def summary(transaction):
    if transaction['schema'] in (IDENTITY_SCHEMA, ANIMATION_SCHEMA):
        from . import world_identity
        return world_identity.summary(transaction)
    if transaction['schema'] == MOVE_SCHEMA:
        return {'operation': 'world.entrance-move', 'index': transaction['index'], 'label': transaction['label'],
                'context': transaction['context'], 'connection': transaction['connection'],
                'moves': [{'header': m['header'], 'id': m['id'], 'from': [m['before']['x'], m['before']['z']],
                           'to': [m['after']['x'], m['after']['z']]} for m in transaction['moves']]}
    if transaction['schema'] == WARP_SCHEMA:
        return {'operation': 'world.connection', 'index': transaction['index'], 'label': transaction['label'],
                'context': transaction['context'],
                'warps': [{k: w[k] for k in ('header', 'id', 'x', 'z', 'destination', 'destination_warp', 'kind')}
                          for w in transaction['warps']]}
    return {'operation': 'world.area', 'index': transaction['index'], 'label': transaction['label'],
            'context': transaction['context'], 'identity': transaction['identity'],
            'header': transaction['header'], 'allocation': transaction['allocation'],
            'cells': [{k: c[k] for k in ('cell', 'map_member', 'source')} | {'closed': len(c['closed'])}
                      for c in transaction['cells']]}


def view(project):
    state = project.composed()
    rows = []
    for identity, area in areas(state).items():
        head = project.header(area['header'])
        rows.append({**copy.deepcopy(area), 'header_record': head['hex'],
                     'owners': {'events': head['event_file'], 'scripts': head['script_file'],
                                'level_script': head['level_script'], 'text': head['text_archive'],
                                'encounters': None if head['wild_pokemon'] == NO_ENCOUNTERS else head['wild_pokemon']}})
    connections = copy.deepcopy(state.get('world', {}).get('connections', []))
    used = len(project._world_headers)
    return {'revision': project.doc['revision'], 'areas': rows, 'connections': connections,
            'capacity': {'created_headers': used, 'limit': world_runtime.MAX_HEADERS,
                         'next_header': world.header_count(project.blob) + used},
            'notes': ['Created headers are resolved by the resident overlay-129 extension (WORLD-ALLOC-001).',
                      'Templates copy visuals only; events, scripts, text and flags start empty.']}


# ---- terrain ---------------------------------------------------------------------
# Three explicit, linked choices per tile: visible ground (surface v4), movement
# (blocked bit) and encounter behavior (0x00 path / 0x02 tall grass). Special
# stock behaviors (entrances, water, ledges...) are never overwritten here.
GROUND_TYPES = {'path': 0x00, 'grass': 0x02}
TERRAIN_FIELDS = {'x', 'z', 'width', 'height', 'tiles', 'material', 'sample', 'ground', 'blocked', 'copy_from', 'label'}


def terrain_request(project, context, request):
    require(isinstance(request, dict) and set(request) <= TERRAIN_FIELDS, 'Unknown terrain fields', 'INVALID_INPUT')
    material, ground, blocked = request.get('material'), request.get('ground'), request.get('blocked')
    stamp = request.get('copy_from')
    require(material is not None or ground is not None or blocked is not None or stamp is not None,
            'Choose a surface, ground behavior, collision change or footprint to copy', 'INVALID_INPUT')
    if stamp is not None:
        # A footprint stamp copies a stock rectangle's type/collision pairs exactly
        # (e.g. a gatehouse with its walls and 0x6E opening); nothing else changes.
        require(isinstance(stamp, dict) and set(stamp) == {'header', 'cell', 'x', 'z'}
                and all(type(stamp[k]) is int for k in ('header', 'x', 'z')),
                'copy_from needs header, cell and the source rectangle x/z', 'INVALID_INPUT')
        require(material is None and ground is None and blocked is None and request.get('tiles') is None,
                'A footprint stamp copies collision and behavior only, over a rectangle', 'INVALID_INPUT')
    require(ground in (None, *GROUND_TYPES), 'Ground behavior is path or grass', 'INVALID_INPUT')
    require(blocked is None or type(blocked) is bool, 'blocked must be true, false or omitted', 'INVALID_INPUT')
    tiles = request.get('tiles')
    rect = [request.get(k) for k in ('x', 'z', 'width', 'height')]
    if tiles is not None:
        require(all(v is None for v in rect), 'Use listed tiles or a rectangle, not both', 'INVALID_INPUT')
        require(isinstance(tiles, list) and 1 <= len(tiles) <= 256 and all(
            isinstance(t, dict) and set(t) == {'x', 'z'} and type(t['x']) is int and type(t['z']) is int for t in tiles),
            'Choose 1..256 tiles with integer x/z', 'INVALID_INPUT')
        coords = [(t['x'], t['z']) for t in tiles]
        require(len(set(coords)) == len(coords), 'Tiles must be distinct', 'INVALID_INPUT')
    else:
        x, z, width, height = rect
        require(all(type(v) is int for v in rect) and 1 <= width <= 16 and 1 <= height <= 16,
                'Choose an integer rectangle up to 16 by 16 tiles', 'INVALID_INPUT')
        coords = [(xx, zz) for zz in range(z, z + height) for xx in range(x, x + width)]
    for x, z in coords:
        world.cell_offset(context, x, z)
    if ground == 'grass':
        require(context['header']['wild_pokemon'] != NO_ENCOUNTERS,
                'Tall grass needs an area with an encounter table', 'NO_ENCOUNTERS')
    surface = None
    if material is not None:
        surface = {'material': material, 'sample': request.get('sample'), 'label': request.get('label'),
                   'tiles': [{'x': x, 'z': z} for x, z in coords]}
    pairs = None
    if stamp is not None:
        source = project.context(header=stamp['header'], cell=stamp['cell'])
        raw = project.member_raw(source['map_member'])
        x0, z0 = coords[0]
        pairs = {}
        for x, z in coords:
            sx, sz = stamp['x'] + x - x0, stamp['z'] + z - z0
            at = world.cell_offset(source, sx, sz)
            pairs[(x, z)] = (source['map_member'], at, raw[at:at + 2])
    return {'coords': coords, 'surface': surface, 'ground': ground, 'blocked': blocked, 'stamp': pairs,
            'label': request.get('label')}


def terrain_permissions(project, context, state, spec):
    """Permission cells for a terrain request, judged against the composed visuals."""
    from . import event_authoring as ev, surface_authoring, mapscene
    rows = ev.records(ev.raw_member(project, context['event_member'], state))
    entrances = {(r['x'], r['z']) for r in rows if r['kind'] == 'warp'}
    raw = project.member_raw(context['map_member'])
    sample = None
    if spec['ground'] is not None:
        from .surface_native import mapping_sampler
        _, blobs = mapscene.tilesets(project, context, state)
        sample = mapping_sampler(surface_authoring.model(project, context, state), blobs['map_tileset'])
    ox, oz = context['origin']
    cells = []
    if state.get('terrain_features'):
        from . import terrain_authoring
        terrain_authoring.require_free(state, context['header']['id'], spec['coords'], 'Terrain behavior')
    for x, z in spec['coords']:
        offset = world.cell_offset(context, x, z)
        pair = state['permissions'].get((context['map_member'], offset), raw[offset:offset + 2])
        require(pair[0] not in WARP_TILES and (x, z) not in entrances,
                f'Tile {x},{z} is an entrance; edit it with the connection tools', 'UNSUPPORTED_TERRAIN')
        kind = pair[0]
        if spec.get('stamp'):
            member, at, stock = spec['stamp'][(x, z)]
            after = state['permissions'].get((member, at), stock)
            if after != pair:
                cells.append({'x': x, 'z': z, 'before': pair.hex(), 'after': after.hex()})
            continue
        if spec['ground'] is not None:
            require(pair[0] in GROUND_TYPES.values(),
                    f'Tile {x},{z} has special behavior {pair[0]:#04x}; only ordinary ground and tall grass change here',
                    'UNSUPPORTED_TERRAIN')
            try:
                top = sample(x - ox, z - oz)['material']
            except EditorError:
                top = None
            grass = (top or '').startswith(OVERLAY_PREFIX)
            # Decorative grass (an explicit border choice) shows tall grass on ordinary ground.
            require(grass == (spec['ground'] == 'grass') or spec.get('decorative') and grass and spec['ground'] == 'path',
                    f'Tile {x},{z}: tall-grass behavior must match visible tall grass', 'UNSUPPORTED_TERRAIN')
            kind = GROUND_TYPES[spec['ground']]
        flags = pair[1] if spec['blocked'] is None else (pair[1] & 0x7F) | (0x80 if spec['blocked'] else 0)
        after = bytes((kind, flags))
        if after != pair:
            cells.append({'x': x, 'z': z, 'before': pair.hex(), 'after': after.hex()})
    return cells


OVERLAY_PREFIX = 'egrass'
