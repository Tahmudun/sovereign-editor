"""Terrain authoring v1: terraces with cliff rims and stairs, ponds, and ambient sound plates.

Family ``canopy-coast-v1`` (docs/TERRAIN_AUTHORING.md). One area operation (kind
``elevation``) is planned once per touched matrix cell: a terrain transaction carries the
cell's model/height (BDHC) change and an ordinary permission transaction carries its
collision/behavior tiles, all in one atomic Project batch. Geometry comes from the pinned
stock donor through terrain_geometry; nothing is painted freehand. Ambient transactions
remove inherited sound plates (WORLD-AUDIO-001). Pure planning; Project owns writes.
"""
import copy
import functools
import json
import struct

from . import authoring, nitro, world
from . import terrain_geometry as tg
from .formats import EditorError, digest, map_sections, require

SCHEMA = 'sovereign-terrain-transaction-v1'
# v2 (TERRAIN-01/02): shaped terraces (union of rectangles, inner corners, second level,
# stairs by tile, one-way ledges) and shaped water (concave shores). v1 rectangles replay
# under v1 rules; their reports and reachability are unchanged.
SHAPE_SCHEMA = 'sovereign-terrain-transaction-v2'
AMBIENT_SCHEMA = 'sovereign-ambient-transaction-v1'
SCHEMAS = (SCHEMA, AMBIENT_SCHEMA, SHAPE_SCHEMA)
FAMILY = 'canopy-coast-v1'
ACTIONS = ('terrace', 'pond', 'terrace_shape', 'water_shape', 'cave_room', 'waterfall')
SHAPES = ('terrace_shape', 'water_shape', 'cave_room')
CAVE_EXITS = 4
FIELDS = {'terrace': {'action', 'x', 'z', 'width', 'height', 'access', 'label'},
          'pond': {'action', 'x', 'z', 'width', 'height', 'traversable', 'label'},
          'terrace_shape': {'action', 'rects', 'level', 'access', 'ledges', 'climbs', 'label'},
          'water_shape': {'action', 'rects', 'traversable', 'label'},
          'cave_room': {'action', 'rects', 'exits', 'encounters', 'label', 'version', 'replaces'},
          'waterfall': {'action', 'x', 'z', 'width', 'upper', 'lower', 'label', 'version', 'replaces'},
          'ambient': {'action', 'cell', 'remove', 'label'}}
SHAPE_RECTS, SHAPE_SPAN, SHAPE_ACCESS, SHAPE_LEDGES, LEDGE_LENGTH = 8, 24, 6, 4, 12
SHAPE_CLIMBS = 4
# Cave geometry versions (original content v1): new cave requests get ``version`` 2 (exit floor
# only under the hole row, R82-CAVE-01); recorded requests without it replay as version 1.
CAVE_VERSION = 2
CAVE_SPAN = 60
# Measured limits (docs/TERRAIN_AUTHORING.md): a terrace top of 1..12 tiles per side
# (footprint up to 14x14 with its rim), a pond of 2..8 tiles per side, at most 4 stairs.
TOP_SIDE = (1, 12)
POND_SIDE = (2, 8)
MAX_ACCESS = 4
# pret sMetatileBehaviorFlags surfable bit (matches tools/area_review.SURFABLE).
SURFABLE = {0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x19, 0x2A, 0x50, 0x51, 0x52, 0x53, 0x73, 0x78, 0x7C}
WARP_BEHAVIORS = {0x69, 0x65, 0x6E}
# WORLD-AUDIO-001: rain's loop (overlay 1 ov01_021EDAB4 -> PlaySE 0x638) and every stock
# sound-plate SE share SDAT player 5 (PLAYER_SE_3, one sequence), so a plate SE replaces the
# rain loop. Weather values that start that loop in the qualified header set:
RAIN_WEATHER = {1: 'SEQ_SE_DP_T_AME'}
PLATE_SOUNDS = ['water flow', 'windmill', 'seashore', 'pillar', 'whirlpool', 'waterfall', 'lava', 'cheers',
                'steam whistle', 'Snorlax snoring', 'motor', 'bells', 'strong wind', 'engine', 'fountain',
                'electric barrier']
PLATE_SEQ = [2132, 2134, 2133, 2135, 2144, 2143, 2145, 2147, 2140, 2137, 2141, 2138, 2139, 2136, 2146, 2148]


def reset(project):
    project._terrain_bdhc = {}


# ---- request validation ----------------------------------------------------------------------

def _rect(request, side):
    values = [request.get(k) for k in ('x', 'z', 'width', 'height')]
    require(all(type(v) is int for v in values), 'x, z, width and height are integers (global tiles)', 'INVALID_INPUT')
    x, z, w, h = values
    require(side[0] <= w <= side[1] and side[0] <= h <= side[1],
            f'Each side is {side[0]}..{side[1]} tiles in this family', 'UNSUPPORTED_TERRAIN')
    return x, z, w, h


def _shape_rects(request, span=SHAPE_SPAN):
    rects = request.get('rects')
    require(isinstance(rects, list) and 1 <= len(rects) <= SHAPE_RECTS
            and all(isinstance(r, list) and len(r) == 4 and all(type(v) is int for v in r) and r[2] >= 1 and r[3] >= 1
                    for r in rects), f'rects lists 1..{SHAPE_RECTS} [x, z, width, height] rectangles (global tiles)',
            'INVALID_INPUT')
    tiles = tg.rect_tiles(rects)
    xs, zs = [t[0] for t in tiles], [t[1] for t in tiles]
    require(max(xs) - min(xs) < span and max(zs) - min(zs) < span,
            f'A shape spans at most {span} x {span} tiles', 'UNSUPPORTED_TERRAIN')
    parts, todo = set(), [min(tiles)]
    while todo:
        t = todo.pop()
        if t in parts:
            continue
        parts.add(t)
        todo.extend(n for n in ((t[0] + 1, t[1]), (t[0] - 1, t[1]), (t[0], t[1] + 1), (t[0], t[1] - 1)) if n in tiles)
    require(parts == tiles, 'The rectangles must form one connected shape', 'UNSUPPORTED_TERRAIN')
    return sorted([list(r) for r in rects])


def _shape(request, action, label, fresh=True):
    rects = _shape_rects(request, CAVE_SPAN if action == 'cave_room' else SHAPE_SPAN)
    if action == 'cave_room':
        exits, encounters = request.get('exits', []), request.get('encounters', True)
        require(isinstance(exits, list) and len(exits) <= CAVE_EXITS
                and all(isinstance(e, dict) and set(e) == {'x', 'z'} and all(type(e[k]) is int for k in e) for e in exits),
                f'Up to {CAVE_EXITS} exits {{x, z}} (a floor tile on a south wall)', 'INVALID_INPUT')
        require(type(encounters) is bool, 'encounters is true (cave floor 0x08) or false', 'INVALID_INPUT')
        spec = {'action': action, 'rects': rects, 'exits': sorted([dict(e) for e in exits], key=lambda e: (e['z'], e['x'])),
                'encounters': encounters, 'label': label}
        version = request.get('version', CAVE_VERSION if fresh else None)
        require(version in (None, CAVE_VERSION), f'Cave geometry version is {CAVE_VERSION}', 'INVALID_INPUT')
        if version is not None:
            spec['version'] = version
        replaces = request.get('replaces')
        require(replaces is None or isinstance(replaces, str) and replaces.startswith('cave_room@'),
                'replaces names the cave feature this request revises (e.g. cave_room@14,3)', 'INVALID_INPUT')
        if replaces is not None:
            spec['replaces'] = replaces
        return spec
    if action == 'water_shape':
        traversable = request.get('traversable', True)
        require(type(traversable) is bool, 'traversable is true (Surf water) or false (decorative)', 'INVALID_INPUT')
        return {'action': action, 'rects': rects, 'traversable': traversable, 'label': label}
    level = request.get('level', 1)
    require(level in (1, 2), 'level is 1 (on ground) or 2 (on a level-1 terrace top)', 'INVALID_INPUT')
    access, ledges = request.get('access', []), request.get('ledges', [])
    require(isinstance(access, list) and len(access) <= SHAPE_ACCESS
            and all(isinstance(a, dict) and set(a) == {'side', 'x', 'z'} and a['side'] in tg.OUTWARD
                    and type(a['x']) is int and type(a['z']) is int for a in access),
            f'Up to {SHAPE_ACCESS} stairs {{side, x, z}} (x, z = the stair tile with the smallest coordinate)',
            'INVALID_INPUT')
    require(isinstance(ledges, list) and len(ledges) <= SHAPE_LEDGES
            and all(isinstance(a, dict) and set(a) == {'side', 'x', 'z', 'length'} and a['side'] in tg.OUTWARD
                    and all(type(a[k]) is int for k in ('x', 'z', 'length')) and 1 <= a['length'] <= LEDGE_LENGTH
                    for a in ledges),
            f'Up to {SHAPE_LEDGES} ledges {{side, x, z, length 1..{LEDGE_LENGTH}}}', 'INVALID_INPUT')
    climbs = request.get('climbs', [])
    require(isinstance(climbs, list) and len(climbs) <= SHAPE_CLIMBS
            and all(isinstance(a, dict) and set(a) == {'side', 'x', 'z'} and a['side'] in tg.OUTWARD
                    and type(a['x']) is int and type(a['z']) is int for a in climbs),
            f'Up to {SHAPE_CLIMBS} Rock Climb faces {{side, x, z}} (one straight rim tile each)', 'INVALID_INPUT')
    key = lambda a: (a['side'], a['x'], a['z'])
    spec = {'action': action, 'rects': rects, 'level': level, 'access': sorted([dict(a) for a in access], key=key),
            'ledges': sorted([dict(a) for a in ledges], key=key), 'label': label}
    if climbs:
        # Only present when used, so recorded terraces keep their exact spec.
        spec['climbs'] = sorted([dict(a) for a in climbs], key=key)
    return spec


def normalise(request, fresh=True):
    """Canonical request. ``fresh`` (a new author request) fills current defaults such as the cave
    geometry version; replay passes False so recorded requests keep their own meaning."""
    require(isinstance(request, dict) and request.get('action') in (*ACTIONS, 'ambient'),
            'Elevation action is terrace, pond, terrace_shape, water_shape or ambient', 'INVALID_INPUT')
    action = request['action']
    require(set(request) <= FIELDS[action], f"{action} fields are {sorted(FIELDS[action])}", 'INVALID_INPUT')
    label = request.get('label')
    require(label is None or isinstance(label, str) and 0 < len(label) <= 160, 'Invalid label', 'INVALID_INPUT')
    if action == 'ambient':
        cell, remove = request.get('cell'), request.get('remove')
        require(isinstance(cell, list) and len(cell) == 2 and all(type(v) is int for v in cell),
                'Ambient edits choose one area cell [x, y]', 'INVALID_INPUT')
        require(isinstance(remove, list) and remove and all(type(i) is int and i >= 0 for i in remove)
                and len(set(remove)) == len(remove), 'remove lists distinct plate indices', 'INVALID_INPUT')
        return {'action': action, 'cell': list(cell), 'remove': sorted(remove), 'label': label}
    if action in SHAPES:
        return _shape(request, action, label, fresh)
    if action == 'waterfall':
        values = [request.get(k) for k in ('x', 'z', 'width', 'upper', 'lower')]
        require(all(type(v) is int for v in values), 'x, z (the upper pool north-west tile), width, upper and lower '
                'are integers', 'INVALID_INPUT')
        x, z, w, hu, hl = values
        require(tg.FALL_WIDTH[0] <= w <= tg.FALL_WIDTH[1] and all(tg.FALL_POOL[0] <= v <= tg.FALL_POOL[1] for v in (hu, hl)),
                f'A waterfall is {tg.FALL_WIDTH[0]}..{tg.FALL_WIDTH[1]} tiles wide with pools '
                f'{tg.FALL_POOL[0]}..{tg.FALL_POOL[1]} tiles deep', 'UNSUPPORTED_TERRAIN')
        spec = {'action': action, 'x': x, 'z': z, 'width': w, 'upper': hu, 'lower': hl, 'label': label}
        # Waterfall geometry v2 (R101-WATERFALL): new requests get it; recorded v1 requests replay as v1.
        version = request.get('version', tg.FALL_VERSION if fresh else None)
        require(version in (None, tg.FALL_VERSION), f'Waterfall geometry version is {tg.FALL_VERSION}', 'INVALID_INPUT')
        if version is not None:
            spec['version'] = version
        replaces = request.get('replaces')
        require(replaces is None or isinstance(replaces, str) and replaces.startswith('waterfall@'),
                'replaces names the waterfall this request revises (e.g. waterfall@15,9)', 'INVALID_INPUT')
        if replaces is not None:
            spec['replaces'] = replaces
        return spec
    if action == 'terrace':
        x, z, w, h = _rect(request, TOP_SIDE)
        access = request.get('access', [])
        require(isinstance(access, list) and len(access) <= MAX_ACCESS, f'Up to {MAX_ACCESS} stairs', 'INVALID_INPUT')
        clean = []
        for item in access:
            require(isinstance(item, dict) and set(item) == {'side', 'offset'} and item['side'] in tg.OUTWARD
                    and type(item['offset']) is int, 'A stair is {side: north|south|east|west, offset}', 'INVALID_INPUT')
            length = w if item['side'] in ('north', 'south') else h
            require(0 <= item['offset'] <= length - tg.STAIR_WIDTH,
                    f"A {tg.STAIR_WIDTH}-tile stair needs offset 0..{length - tg.STAIR_WIDTH} on the "
                    f"{item['side']} side (the side is {length} tiles)", 'UNSUPPORTED_ACCESS')
            clean.append({'side': item['side'], 'offset': item['offset']})
        clean.sort(key=lambda a: (a['side'], a['offset']))
        for a, b in zip(clean, clean[1:]):
            require(a['side'] != b['side'] or b['offset'] - a['offset'] >= tg.STAIR_WIDTH + 1,
                    'Stairs on one side need a rim tile between them', 'UNSUPPORTED_ACCESS')
        return {'action': action, 'x': x, 'z': z, 'width': w, 'height': h, 'access': clean, 'label': label}
    x, z, w, h = _rect(request, POND_SIDE)
    traversable = request.get('traversable', True)
    require(type(traversable) is bool, 'traversable is true (Surf water) or false (decorative)', 'INVALID_INPUT')
    return {'action': action, 'x': x, 'z': z, 'width': w, 'height': h, 'traversable': traversable, 'label': label}


def is_terrace(spec): return spec['action'] in ('terrace', 'terrace_shape')


def layout(spec):
    if spec['action'] == 'cave_room':
        from . import cave_geometry
        return cave_geometry.layout(spec)
    if spec['action'] == 'terrace_shape':
        return tg.shaped_terrace_layout(spec)
    if spec['action'] == 'water_shape':
        return tg.shaped_water_layout(spec)
    if spec['action'] == 'waterfall':
        return tg.waterfall_layout(spec)
    return tg.terrace_layout(spec) if spec['action'] == 'terrace' else tg.water_layout(spec)


def claimed(spec):
    """Tiles a feature owns for overlap refusal: footprint, ring and stair landings."""
    lay = layout(spec)
    if is_terrace(spec) or spec['action'] == 'cave_room':
        return lay['footprint'] | lay['landings']
    return lay['footprint'] | lay['ring']


def clear_rects(spec):
    if spec['action'] in SHAPES or spec['action'] == 'waterfall':
        return tg.rectangles(layout(spec)['footprint'])
    x, z, w, h = spec['x'], spec['z'], spec['width'], spec['height']
    if spec['action'] == 'terrace':
        return [(x - 1, z - 1, x + w + 1, z + h + 1)]
    return [(x, z, x + w, z + h)]


def guard_rects(spec):
    lip = 0.3
    if spec['action'] == 'waterfall':
        lay = layout(spec)
        out = []
        for (x, z), side in tg.shore_edges(lay['lower'] | set(lay['fall'])):
            if (x, z) in lay['lower']:
                out.append({'west': (x - lip, z - lip, x, z + 1 + lip), 'east': (x + 1, z - lip, x + 1 + lip, z + 1 + lip),
                            'north': (x - lip, z - lip, x + 1 + lip, z), 'south': (x - lip, z + 1, x + 1 + lip, z + 1 + lip)}[side])
        return out
    if spec['action'] == 'water_shape':
        out = []
        for (x, z), side in tg.shore_edges(layout(spec)['water']):
            out.append({'west': (x - lip, z - lip, x, z + 1 + lip), 'east': (x + 1, z - lip, x + 1 + lip, z + 1 + lip),
                        'north': (x - lip, z - lip, x + 1 + lip, z), 'south': (x - lip, z + 1, x + 1 + lip, z + 1 + lip)}[side])
        return out
    if spec['action'] != 'pond':
        return []
    x, z, w, h = spec['x'], spec['z'], spec['width'], spec['height']
    lip = 0.3
    return [(x - lip, z - lip, x + w + lip, z), (x - lip, z + h, x + w + lip, z + h + lip),
            (x - lip, z, x, z + h), (x + w, z, x + w + lip, z + h)]


# ---- composed reads --------------------------------------------------------------------------

def area_of(project, state, header):
    from . import world_authoring
    for key, area in world_authoring.areas(state).items():
        if area['header'] == header:
            return key, area
    raise EditorError('UNSUPPORTED_TERRAIN', 'Terrain authoring edits project-created areas only')


def cell_context(project, header, x, z):
    from .border_authoring import cell_of
    try:
        cx, cy = cell_of(project, header, x, z)
    except EditorError as exc:
        raise EditorError('UNSUPPORTED_TERRAIN', f'Tile {x},{z} lies outside this area') from exc
    return project.context(header=header, cell=[cx, cy])


def bdhc_bytes(project, member):
    registry = getattr(project, '_terrain_bdhc', {})
    if member in registry:
        return registry[member]
    raw = project.member_raw(member)
    return raw[map_sections(raw)['terrain_offset']:]


def tile_height(project, context, x, z):
    """Runtime-mirrored height (tile units) at a global tile centre, or None."""
    ox, oz = context['origin']
    table = tg.parse_bdhc(bdhc_bytes(project, context['map_member']))
    return tg.height_at(table, x - ox - 16 + 0.5, z - oz - 16 + 0.5)


def sloped(project, context, x, z):
    """True when a sloped (stair/ramp) height plate covers the tile centre."""
    ox, oz = context['origin']
    table = tg.parse_bdhc(bdhc_bytes(project, context['map_member']))
    X, Z = (x - ox - 16 + 0.5) * tg.FX, (z - oz - 16 + 0.5) * tg.FX
    return any(p['normal'] != (0, 4096, 0) and min(p['rect'][0], p['rect'][2]) <= X <= max(p['rect'][0], p['rect'][2])
               and min(p['rect'][1], p['rect'][3]) <= Z <= max(p['rect'][1], p['rect'][3]) for p in table['plates'])


def pair_at(project, state, context, x, z):
    raw = project.member_raw(context['map_member'])
    offset = world.cell_offset(context, x, z)
    return state['permissions'].get((context['map_member'], offset), raw[offset:offset + 2]), offset


def features(state, member):
    return state.get('terrain_features', {}).get(member, [])


def area_features(project, state, header):
    seen, result = set(), []
    for member, items in state.get('terrain_features', {}).items():
        for item in items:
            if item['header'] == header and item['id'] not in seen:
                seen.add(item['id'])
                result.append(item)
    return result


# ---- composition hooks -----------------------------------------------------------------------

@functools.lru_cache(maxsize=64)
def _geometry(spec_json, ground, lib_key):
    spec = json.loads(spec_json)
    lib = _LIBS[lib_key]
    if spec['action'] == 'cave_room':
        from . import cave_geometry
        return cave_geometry.pieces(lib, spec, ground)
    if spec['action'] == 'terrace_shape':
        return tg.shaped_terrace_pieces(lib, spec, ground)
    if spec['action'] == 'water_shape':
        return tg.shaped_water_pieces(lib, spec, ground)
    if spec['action'] == 'waterfall':
        return tg.waterfall_pieces(lib, spec, ground)
    return tg.terrace_pieces(lib, spec, ground) if spec['action'] == 'terrace' else tg.water_pieces(lib, spec, ground)


_LIBS = {}


def _lib(project):
    lib = tg.library(project)
    _LIBS[id(lib)] = lib
    return lib, id(lib)


def _cave_lib(project):
    from . import cave_geometry
    lib = cave_geometry.library(project)
    _LIBS[id(lib)] = lib
    return lib, id(lib)


_STOCK_PALETTES = {}


def stock_palettes(project, map_tileset):
    """texture -> palette as the stock map models of this map tileset bind them (the names are
    irregular, e.g. sea_on -> sea_f02_pl, grass01gs -> grass01); the most common binding wins."""
    # A function of the baseline ROM alone: shared by every Project (and trial fork) of that baseline.
    key = (project.doc['baseline']['sha256'], map_tileset)
    cache = _STOCK_PALETTES
    if key in cache:
        return cache[key]
    from . import mapscene, surface_native
    from .formats import member_count, resource
    counts = {}
    blob = project.blob
    tilesets = {}
    for h in range(world.header_count(blob)):
        head = world.read_header(blob, h)
        area = world.read_area_data(blob, head['area_data'])
        if area['map_tileset'] != map_tileset or head['matrix'] in tilesets:
            continue
        tilesets[head['matrix']] = True
        grid = world.read_matrix(blob, head['matrix'])
        members = {m for row in grid['maps'] for m in row if m != world.EMPTY}
        if len(members) > 40:
            members = {m for cy, row in enumerate(grid['maps']) for cx, m in enumerate(row)
                       if m != world.EMPTY and (not grid['has_headers'] or grid['headers'][cy][cx] == h)}
        for m in sorted(members)[:40]:
            try:
                dec = surface_native._decode(map_data_model(blob, m), resource(blob, 'a/0/4/4', map_tileset)[1])
            except EditorError:
                continue
            for part in dec[6].values():
                key = (part.material['texture_name'], part.material['palette_name'])
                counts[key] = counts.get(key, 0) + 1
        if len(counts) > 400:
            break
    best = {}
    for (tex, pal), n in sorted(counts.items(), key=lambda kv: -kv[1]):
        best.setdefault(tex, pal)
    cache[key] = best
    return best


def map_data_model(blob, member):
    from .formats import map_data, resource
    return map_data(resource(blob, world.MAP_ARCHIVE, member)[1])[2]


def compose_model(project, context, state, raw):
    """Apply a cell's terrain features to its (already surfaced) model bytes."""
    items = features(state, context['map_member'])
    if not items:
        return raw
    from . import mapscene
    lib, key = _lib(project)
    cave_key = _cave_lib(project)[1] if any(f['spec']['action'] == 'cave_room' for f in items) else None
    refs, blobs = mapscene.tilesets(project, context, state)
    palettes = json.dumps(stock_palettes(project, context['area_data']['map_tileset']), sort_keys=True)
    return _apply(raw, blobs['map_tileset'], tuple(context['origin']),
                  json.dumps([{'spec': f['spec'], 'ground': f['ground']} for f in items], sort_keys=True), key, cave_key,
                  palettes)


@functools.lru_cache(maxsize=32)
def _apply(raw, tileset, origin, items_json, lib_key, cave_key=None, palettes='{}'):
    lib = _LIBS[lib_key]
    items = []
    for item in json.loads(items_json):
        spec = item['spec']
        if spec['action'] == 'cave_room':
            from . import cave_geometry as cg
            geometry = _geometry(json.dumps(spec, sort_keys=True), item['ground'], cave_key)
            items.append({'kind': 'cave', 'ground': item['ground'], 'geometry': geometry, 'clear': clear_rects(spec),
                          'guard': [], 'lib': _LIBS[cave_key], 'needed': cg.MATERIALS, 'clearable': cg.CLEARABLE,
                          'levels': (item['ground'] + 32, item['ground'] + 32)})
            continue
        geometry = _geometry(json.dumps(spec, sort_keys=True), item['ground'], lib_key)
        items.append({'kind': 'terrace' if is_terrace(spec) else 'pond', 'ground': item['ground'],
                      'geometry': geometry, 'clear': clear_rects(spec), 'guard': guard_rects(spec),
                      **({'needed': tg.TERRACE_MATERIALS + tg.WATER_MATERIALS} if spec['action'] == 'waterfall' else {})})
    return tg.apply_model(raw, tileset, origin, items, lib, json.loads(palettes))


def compose_bdhc(project, context, before, spec, ground):
    """This cell's height table after one feature (fragments keep order; new plates follow)."""
    table = tg.parse_bdhc(before)
    plates = table['plates']
    ox, oz = context['origin']
    lo = lambda v, o: tg.local_fx(v, o)
    cell = (ox, oz, ox + 32, oz + 32)

    def clip(rect):
        x0, z0, x1, z1 = max(rect[0], cell[0]), max(rect[1], cell[1]), min(rect[2], cell[2]), min(rect[3], cell[3])
        return (x0, z0, x1, z1) if x1 > x0 and z1 > z0 else None

    units = ground / 16
    added = []
    if spec['action'] in SHAPES:
        lay = layout(spec)
        if spec['action'] == 'cave_room':
            for rect in tg.rectangles(lay['floor']):
                floor = clip(rect)
                if floor:
                    fx = (lo(floor[0], ox), lo(floor[1], oz), lo(floor[2], ox), lo(floor[3], oz))
                    plates = tg.subtract(plates, fx)
                    added.append({'rect': fx, **tg.horizontal(units)})
            return tg.build_bdhc(plates + added)
        if spec['action'] == 'terrace_shape':
            tops = [clip(r) for r in tg.rectangles(lay['top'])]
            stairs = []
            for stair in lay['stairs']:
                xs = [t[0] for t in stair['tiles']]
                zs = [t[1] for t in stair['tiles']]
                rect = (min(xs), min(zs), max(xs) + 1, max(zs) + 1)
                if clip(rect) == rect:
                    stairs.append((stair['side'], rect))
            for climb in lay.get('climbs', []):
                # A Rock Climb tile gets the stair's slope plate (Cherrygrove's stock climb tiles
                # are sloped plates between the base and the top).
                t = climb['tiles'][0]
                rect = (t[0], t[1], t[0] + 1, t[1] + 1)
                if clip(rect) == rect:
                    stairs.append((climb['side'], rect))
            for rect in [r for r in tops if r] + [r for _, r in stairs]:
                plates = tg.subtract(plates, (lo(rect[0], ox), lo(rect[1], oz), lo(rect[2], ox), lo(rect[3], oz)))
            for rect in [r for r in tops if r]:
                added.append({'rect': (lo(rect[0], ox), lo(rect[1], oz), lo(rect[2], ox), lo(rect[3], oz)),
                              **tg.horizontal(units + 1)})
            for side, rect in stairs:
                edge = {'south': (0, rect[1] - oz - 16), 'north': (0, rect[3] - oz - 16),
                        'east': (rect[0] - ox - 16, 0), 'west': (rect[2] - ox - 16, 0)}[side]
                added.append({'rect': (lo(rect[0], ox), lo(rect[1], oz), lo(rect[2], ox), lo(rect[3], oz)),
                              **tg.slope(side, edge, units + 1)})
        else:
            for rect in tg.rectangles(lay['water']):
                water = clip(rect)
                if water:
                    fx = (lo(water[0], ox), lo(water[1], oz), lo(water[2], ox), lo(water[3], oz))
                    plates = tg.subtract(plates, fx)
                    added.append({'rect': fx, **tg.horizontal(units - tg.WATER_DROP / 16)})
        return tg.build_bdhc(plates + added)
    if spec['action'] == 'waterfall':
        lay = layout(spec)
        groups = [(lay['top'] - lay['upper'], units + 1), (lay['upper'] | set(lay['fall']), units + 1 - tg.WATER_DROP / 16),
                  (lay['lower'], units - tg.WATER_DROP / 16)]
        for tiles, height in groups:
            for rect in tg.rectangles(tiles):
                r = clip(rect)
                if r:
                    fx = (lo(r[0], ox), lo(r[1], oz), lo(r[2], ox), lo(r[3], oz))
                    plates = tg.subtract(plates, fx)
                    added.append({'rect': fx, **tg.horizontal(height)})
        return tg.build_bdhc(plates + added)
    x, z, w, h = spec['x'], spec['z'], spec['width'], spec['height']
    if spec['action'] == 'terrace':
        top = clip((x, z, x + w, z + h))
        stairs = []
        for stair in tg.terrace_layout(spec)['stairs']:
            xs = [t[0] for t in stair['tiles']]
            zs = [t[1] for t in stair['tiles']]
            rect = (min(xs), min(zs), max(xs) + 1, max(zs) + 1)
            if clip(rect) == rect:
                stairs.append((stair['side'], rect))
        for rect in [top] + [r for _, r in stairs]:
            if rect:
                plates = tg.subtract(plates, (lo(rect[0], ox), lo(rect[1], oz), lo(rect[2], ox), lo(rect[3], oz)))
        if top:
            added.append({'rect': (lo(top[0], ox), lo(top[1], oz), lo(top[2], ox), lo(top[3], oz)),
                          **tg.horizontal(units + 1)})
        for side, rect in stairs:
            edge = {'south': (0, rect[1] - oz - 16), 'north': (0, rect[3] - oz - 16),
                    'east': (rect[0] - ox - 16, 0), 'west': (rect[2] - ox - 16, 0)}[side]
            added.append({'rect': (lo(rect[0], ox), lo(rect[1], oz), lo(rect[2], ox), lo(rect[3], oz)),
                          **tg.slope(side, edge, units + 1)})
    else:
        water = clip((x, z, x + w, z + h))
        if water:
            fx = (lo(water[0], ox), lo(water[1], oz), lo(water[2], ox), lo(water[3], oz))
            plates = tg.subtract(plates, fx)
            added.append({'rect': fx, **tg.horizontal(units - tg.WATER_DROP / 16)})
    return tg.build_bdhc(plates + added)


# ---- planning --------------------------------------------------------------------------------

def _events(project, state, header):
    """Global tiles occupied by events (NPCs, warps, triggers, signs) in the area."""
    from . import event_authoring as ev
    tiles = {}
    _, area = area_of(project, state, header)
    for cell in area['cells']:
        ctx = project.context(header=header, cell=cell['cell'])
        for r in ev.records(ev.raw_member(project, ctx['event_member'], state)):
            if r.get('x') is None or r.get('z') is None:
                continue
            w, h = r.get('width') or 1, r.get('height') or 1
            for xx in range(r['x'], r['x'] + w):
                for zz in range(r['z'], r['z'] + h):
                    tiles.setdefault((xx, zz), r['kind'])
    return tiles


def _objects(project, state, header):
    from . import scenery
    tiles = set()
    _, area = area_of(project, state, header)
    for cell in area['cells']:
        ctx = project.context(header=header, cell=cell['cell'])
        for obj in scenery.table_for(project, ctx, state).values():
            xyz = struct.unpack_from('<3i', obj['raw'], 4)
            gx = ctx['origin'][0] + 16 + xyz[0] / 65536
            gz = ctx['origin'][1] + 16 + xyz[2] / 65536
            tiles.add((int(gx // 1), int(gz // 1)))
    return tiles


def reachable(project, state, header, heights=None, pairs=None, ledges=False):
    """Tiles reachable on foot (and separately by Surf) from the area's warp tiles, with the
    runtime step rule: permission collision bit clear and |dh| < 1.25 tile between centres.
    ``ledges`` (shape features, v2) also follows stock one-way jumps: stepping onto a jump tile
    in its direction lands two tiles on. v1 features keep the historical rule."""
    _, area = area_of(project, state, header)
    ctx_of, walk, water, height, jumps, falls = {}, set(), set(), {}, {}, set()
    for cell in area['cells']:
        ctx = project.context(header=header, cell=cell['cell'])
        ox, oz = ctx['origin']
        table = tg.parse_bdhc(bdhc_bytes(project, ctx['map_member']))
        raw = project.member_raw(ctx['map_member'])
        for zz in range(oz, oz + 32):
            for xx in range(ox, ox + 32):
                t = (xx, zz)
                if pairs is not None and t in pairs:
                    pair = pairs[t]
                else:
                    offset = world.cell_offset(ctx, xx, zz)
                    pair = state['permissions'].get((ctx['map_member'], offset), raw[offset:offset + 2])
                if heights is not None and t in heights:
                    hgt = heights[t]
                else:
                    hgt = tg.height_at(table, xx - ox - 16 + 0.5, zz - oz - 16 + 0.5)
                height[t] = hgt
                if ledges and pair[0] in tg.JUMP_DIRECTION:
                    jumps[t] = tg.JUMP_DIRECTION[pair[0]]
                    continue
                if pair[0] == 0x13 and pair[1] & 0x80:
                    falls.add(t)              # blocked waterfall row: crossed only by the Waterfall move
                if pair[1] & 0x80 or hgt is None:
                    continue
                (water if pair[0] in SURFABLE else walk).add(t)
    # Arrivals: warp tiles themselves (exit mats) or, for blocked doors, their walkable neighbours.
    warps = [t for t, kind in _events(project, state, header).items() if kind == 'warp']
    starts = sorted({n for x, z in warps for n in ((x, z), (x + 1, z), (x - 1, z), (x, z + 1), (x, z - 1))
                     if n in walk})
    require(starts, 'This area has no walkable arrival next to a warp; reachability cannot be checked',
            'UNSUPPORTED_TERRAIN')

    def flood(allowed, seeds):
        seen, todo = set(seeds), list(seeds)
        while todo:
            x, z = todo.pop()
            for n in ((x + 1, z), (x - 1, z), (x, z + 1), (x, z - 1)):
                d = (n[0] - x, n[1] - z)
                if jumps.get(n) == d:
                    land = (n[0] + d[0], n[1] + d[1])
                    if land in allowed and land not in seen:
                        seen.add(land)
                        todo.append(land)
                    continue
                if n in falls and d[0] == 0:
                    # The stock Waterfall task moves two tiles north or south across the fall row.
                    land = (n[0], n[1] + d[1])
                    if land in allowed and land in water and (x, z) in water and land not in seen:
                        seen.add(land)
                        todo.append(land)
                    continue
                if n in allowed and n not in seen and abs(height[n] - height[(x, z)]) < tg.BLOCK_DELTA:
                    seen.add(n)
                    todo.append(n)
        return seen
    foot = flood(walk, starts)
    surf = flood(walk | water, list(foot))
    return {'foot': foot, 'surf': surf - foot, 'starts': sorted(starts)}


def plan_feature(project, state, header, spec):
    """Global validation of a terrace/pond; returns per-cell parts and a report."""
    from . import world_authoring
    area_key, area = area_of(project, state, header)
    lay = layout(spec)
    foot = lay['footprint']
    extra = lay['landings'] if is_terrace(spec) else lay['ring']
    shaped = spec['action'] in SHAPES
    base = None
    replaced = None
    if spec.get('replaces') and spec['action'] == 'waterfall':
        # A waterfall revision (R101-WATERFALL): same pools and fall row, new geometry version; the
        # recorded feature is replaced in composition, its history kept.
        replaced = next((f for f in area_features(project, state, header) if f['id'] == spec['replaces']), None)
        require(replaced is not None and replaced['spec']['action'] == 'waterfall',
                f"No waterfall {spec['replaces']} in this area", 'NOT_FOUND')
        keys = ('x', 'z', 'width', 'upper', 'lower')
        require(all(replaced['spec'][k] == spec[k] for k in keys),
                'A waterfall revision keeps its position, width and pools; it changes the geometry version',
                'UNSUPPORTED_TERRAIN')
        require(replaced['spec'].get('version', 1) != spec.get('version', 1),
                f"{spec['replaces']} already uses this geometry version", 'NO_CHANGE')
    elif spec.get('replaces'):
        # A cave revision (R82-CAVE-01): same rooms, new exits/encounters/geometry version; the
        # recorded feature is replaced in composition, its history kept.
        replaced = next((f for f in area_features(project, state, header) if f['id'] == spec['replaces']), None)
        require(replaced is not None and replaced['spec']['action'] == 'cave_room',
                f"No cave feature {spec['replaces']} in this area", 'NOT_FOUND')
        require(replaced['spec']['rects'] == spec['rects'],
                'A cave revision keeps its rooms (rects); it changes exits, encounters and the geometry version',
                'UNSUPPORTED_TERRAIN')
    if spec['action'] == 'terrace_shape' and spec['level'] == 2:
        # A second level stands wholly on one existing terrace top, stairs/ledges landing on it.
        for other in area_features(project, state, header):
            if is_terrace(other['spec']) and foot | extra <= layout(other['spec'])['top']:
                base = other
        require(base is not None, 'A level-2 terrace (with its rim and stair/ledge landings) must stand on one '
                'level-1 terrace top', 'UNSUPPORTED_TERRAIN')
    cells, ctxs = {}, {}
    for t in sorted(foot | extra):
        ctx = cell_context(project, header, *t)
        ctxs[t] = ctx
        cells.setdefault((ctx['cell']['x'], ctx['cell']['y']), ctx)
    require(len(cells) <= 4, 'A terrain feature may touch at most four cells', 'UNSUPPORTED_TERRAIN')
    grid = project.matrix_data(project.header(header)['matrix'])
    if grid.get('altitudes'):
        alts = {grid['altitudes'][cy][cx] for cx, cy in cells}
        require(len(alts) == 1, 'The touched cells have different matrix altitudes', 'UNSUPPORTED_TERRAIN')
    if is_terrace(spec):
        for stair in lay['stairs']:
            owners = {(ctxs[t]['cell']['x'], ctxs[t]['cell']['y']) for t in stair['tiles']}
            require(len(owners) == 1, f"The {stair['side']} stair would straddle a cell seam; move it",
                    'UNSUPPORTED_ACCESS')
        if lay.get('climbs'):
            from . import mapscene
            _, blobs = mapscene.tilesets(project, next(iter(cells.values())), state)
            names, _ = nitro.texture_set(blobs['map_tileset'])
            require(tg.CLIMB_MATERIAL in names, 'This area’s map tileset has no Rock Climb face (r_climb)',
                    'UNSUPPORTED_TERRAIN')
    for other in area_features(project, state, header):
        if other is replaced:
            continue
        overlap = sorted(claimed(spec) & claimed(other['spec']))
        if overlap and other is base and set(overlap) <= layout(other['spec'])['top']:
            continue
        if overlap:
            raise EditorError('UNSUPPORTED_TERRAIN', f"Overlaps terrain feature {other['id']} at {overlap[0]}")
    events, objects = _events(project, state, header), _objects(project, state, header)
    if spec['action'] == 'cave_room':
        return _plan_cave(project, state, spec, lay, cells, ctxs, events, objects, area_key, replaced)
    if replaced is not None and spec['action'] == 'waterfall':
        # A revision keeps the recorded footprint (its tiles now hold the old waterfall).
        ground = replaced['ground']
    else:
        grounds = set()
        for t in sorted(foot | extra):
            ctx = ctxs[t]
            pair, _ = pair_at(project, state, ctx, *t)
            hgt = tile_height(project, ctx, *t)
            in_foot = t in foot
            if in_foot or is_terrace(spec):
                require(t not in events, f'Tile {t[0]},{t[1]} holds a {events.get(t)}; move it first', 'UNSUPPORTED_TERRAIN')
            if in_foot:
                require(t not in objects, f'Tile {t[0]},{t[1]} holds a placed object; move it first', 'UNSUPPORTED_TERRAIN')
                require(pair[0] == 0x00 and not pair[1] & 0x80,
                        f'Tile {t[0]},{t[1]} is {pair.hex()}; the family reshapes walkable ordinary ground '
                        '(behavior 00, any footstep sound) only', 'UNSUPPORTED_TERRAIN')
                require(hgt is not None, f'Tile {t[0]},{t[1]} has no height plate', 'UNSUPPORTED_TERRAIN')
                grounds.add(round(hgt * 16, 3))
            elif is_terrace(spec):
                require(not pair[1] & 0x80 and pair[0] not in SURFABLE | WARP_BEHAVIORS,
                        f'Stair landing {t[0]},{t[1]} is blocked or special ({pair.hex()})', 'UNSUPPORTED_ACCESS')
                require(hgt is not None, f'Stair landing {t[0]},{t[1]} has no height plate', 'UNSUPPORTED_ACCESS')
                grounds.add(round(hgt * 16, 3))
        # An existing stair keeps its landing: nothing in the footprint may touch a sloped tile.
        for t in sorted(foot | extra):
            for n in ((t[0] + 1, t[1]), (t[0] - 1, t[1]), (t[0], t[1] + 1), (t[0], t[1] - 1), t):
                try:
                    nctx = ctxs.get(n) or cell_context(project, header, *n)
                except EditorError:
                    continue
                require(not sloped(project, nctx, *n) or (n not in foot and t not in foot),
                        f'Tile {t[0]},{t[1]} borders the existing stair at {n[0]},{n[1]}; keep its landing clear',
                        'UNSUPPORTED_ACCESS')
        require(len(grounds) == 1, f'The footprint must be one flat ground height (found {sorted(grounds)})',
                'UNSUPPORTED_HEIGHT')
        ground = grounds.pop()
        require(ground == int(ground), 'Ground height is not a whole model unit', 'UNSUPPORTED_HEIGHT')
        ground = int(ground)
        if not is_terrace(spec):
            rings = []
            for t in sorted(lay['ring']):
                pair, _ = pair_at(project, state, ctxs[t], *t)
                hgt = tile_height(project, ctxs[t], *t)
                require(pair[0] not in SURFABLE, f'Tile {t[0]},{t[1]} is already water; ponds do not join other water '
                        'in this family', 'UNSUPPORTED_WATER')
                if not pair[1] & 0x80 and hgt is not None and round(hgt * 16) == ground:
                    rings.append(t)
            require(not spec.get('traversable', True) or rings,
                    'A Surf pond needs at least one walkable shore tile at ground height', 'UNSUPPORTED_WATER')
    # Permissions after the feature.
    pairs = {}
    if is_terrace(spec):
        stair_tiles = {t for s in lay['stairs'] for t in s['tiles']}
        ledge_tiles = {t: bytes((tg.JUMPS[e['side']], 0x80)) for e in lay.get('ledges', []) for t in e['tiles']}
        ledge_tiles.update({t: bytes((c['behavior'], 0x80)) for c in lay.get('climbs', []) for t in c['tiles']})
        for t in lay['top']:
            pairs[t] = tg.PAIRS['ground']
        for t in lay['ring']:
            pairs[t] = tg.PAIRS['stair'] if t in stair_tiles else ledge_tiles.get(t, tg.PAIRS['rim'])
    elif spec['action'] == 'waterfall':
        # Upper and lower Surf water, the 0x13 fall row between them, the terrace rim and margin.
        for t in lay['top'] - lay['upper']:
            pairs[t] = tg.PAIRS['ground']
        for t in lay['rim']:
            pairs[t] = tg.PAIRS['rim']
        for t in lay['upper'] | lay['lower']:
            pairs[t] = tg.PAIRS['water']
        for t in lay['fall']:
            pairs[t] = tg.fall_pair(spec)
    else:
        for t in lay['water']:
            pairs[t] = tg.PAIRS['water'] if spec['traversable'] else tg.PAIRS['decorative_water']
    heights = {}
    unit = ground / 16
    if is_terrace(spec):
        for t in lay['top']:
            heights[t] = unit + 1
        for t in {t for s in lay['stairs'] + lay.get('climbs', []) for t in s['tiles']}:
            heights[t] = unit + 0.5
    elif spec['action'] == 'waterfall':
        for t in lay['top'] - lay['upper']:
            heights[t] = unit + 1
        for t in lay['upper'] | set(lay['fall']):
            heights[t] = unit + 1 - tg.WATER_DROP / 16
        for t in lay['lower']:
            heights[t] = unit - tg.WATER_DROP / 16
    else:
        for t in lay['water']:
            heights[t] = unit - tg.WATER_DROP / 16
    before = reachable(project, state, header, ledges=shaped)
    after = reachable(project, state, header, heights, pairs, ledges=shaped)
    lost = sorted((before['foot'] - after['foot']) - foot)
    if lost:
        raise EditorError('STRANDED', f'The change would strand reachable tile {lost[0][0]},{lost[0][1]} '
                          f'({len(lost)} tile(s)); add or move a stair')
    report = {'family': FAMILY, 'action': spec['action'], 'ground_height': ground,
              'cells': sorted([list(c) for c in cells]), 'footprint_tiles': len(foot)}
    if is_terrace(spec):
        report.update(top_height=ground + tg.RISE, rim_tiles=len(lay['ring']) - 3 * len(lay['stairs']),
                      stairs=[{'side': s['side'], 'tiles': [list(t) for t in s['tiles']],
                               'landing': [list(t) for t in s['landing']]} for s in lay['stairs']],
                      top_reachable=bool(lay['top'] & after['foot']),
                      seams=sorted({'z=%d' % (c[1] * 32) for c in cells if (c[0], c[1] - 1) in cells}
                                   | {'x=%d' % (c[0] * 32) for c in cells if (c[0] - 1, c[1]) in cells}))
        if shaped:
            roles = [r[0] for r in lay['roles'].values()]
            report.update(level=spec['level'], base=base['id'] if base else None,
                          outer_corners=roles.count('outer'), inner_corners=roles.count('inner'),
                          ledges=[{'side': e['side'], 'tiles': [list(t) for t in e['tiles']],
                                   'landing': [list(t) for t in e['landing']], 'jump': tg.JUMPS[e['side']]}
                                  for e in lay['ledges']],
                          ledges_reachable=all(any(tuple(t) in after['foot'] for t in e['landing'])
                                               for e in lay['ledges']) if lay['ledges'] else None)
            if lay.get('climbs'):
                report['climbs'] = [{'side': c['side'], 'tile': list(c['tiles'][0]), 'landing': list(c['landing'][0]),
                                     'behavior': f"{c['behavior']:#x} Rock Climb (Earth Badge, std 10010)",
                                     'landing_reachable': tuple(c['landing'][0]) in after['foot']}
                                    for c in lay['climbs']]
    elif spec['action'] == 'waterfall':
        entries = sorted(t for t in lay['ring'] if t in after['foot'])
        report.update(top_height=ground + tg.RISE, upper_water_height=ground + tg.RISE - tg.WATER_DROP,
                      lower_water_height=ground - tg.WATER_DROP,
                      fall=[list(t) for t in lay['fall']],
                      fall_behavior=('0x13 Waterfall, blocked (13 80): A or the party menu climbs two tiles '
                                     'north (Rising Badge + Waterfall); surfing south onto it descends'
                                     if spec.get('version', 1) >= 2 else
                                     '0x13 Waterfall (surfing, Rising Badge, std 10012): up two tiles north, down '
                                     'two tiles south'),
                      surf_entries=len(entries), lower_reachable=bool(lay['lower'] & after['surf']),
                      upper_reachable_via_fall=bool(lay['upper'] & after['surf']))
        if spec.get('version', 1) >= 2:
            report['geometry_version'] = spec['version']
    else:
        entries = sorted(t for t in lay['ring'] if t in after['foot'])
        report.update(water_height=ground - tg.WATER_DROP, traversable=spec['traversable'],
                      water='Surf water (behavior 0x15, stock sea rules)' if spec['traversable']
                      else 'decorative water (blocked, no Surf)',
                      surf_entries=len(entries) if spec['traversable'] else 0,
                      surf_reachable=bool(lay['water'] & after['surf']) if spec['traversable'] else False)
        if shaped:
            corners = {'convex': 0, 'concave': 0}
            for (x, z), side in tg.shore_edges(lay['water']):
                n = tg.OUTWARD[side]
                for a in ((n[1], n[0]), (-n[1], -n[0])):
                    u = (x + a[0], z + a[1])
                    if u not in lay['water']:
                        corners['convex'] += 1
                    elif (u[0] + n[0], u[1] + n[1]) in lay['water']:
                        corners['concave'] += 1
            report.update(shore_edges=len(tg.shore_edges(lay['water'])), convex_corner_ends=corners['convex'],
                          concave_corner_ends=corners['concave'])
    return {'cells': cells, 'ctxs': ctxs, 'pairs': pairs, 'ground': ground, 'report': report,
            'area': area_key, 'layout': lay}


def _plan_cave(project, state, spec, lay, cells, ctxs, events, objects, area_key, replaced=None):
    """Cave family: carve into void (no permission, height or event) of a cell holding the donor
    materials; the floor sits at the donor floor height. A revision may rewrite the tiles of the
    cave it replaces: events stay only on floor, and a warp only on a tile that is still an exit."""
    from . import cave_geometry as cg
    old = claimed(replaced['spec']) if replaced else set()
    exits = set(lay['exits'])
    for t in sorted(lay['footprint']):
        if t in old:
            kind = events.get(t)
            require(not kind or t in lay['floor'] and (kind != 'warp' or t in exits),
                    f"Tile {t[0]},{t[1]} holds a {kind} that the revised cave would "
                    + ('close; remove or move its connection first' if kind == 'warp' else 'wall in; move it first'),
                    'UNSUPPORTED_TERRAIN')
            require(t not in objects or t in lay['floor'], f'Tile {t[0]},{t[1]} holds a placed object', 'UNSUPPORTED_TERRAIN')
            continue
        pair, _ = pair_at(project, state, ctxs[t], *t)
        require(t not in events and t not in objects, f'Tile {t[0]},{t[1]} holds an event or object', 'UNSUPPORTED_TERRAIN')
        # Void (00 00) or rock (00 80, e.g. the closed boundary of a created cell), with no height.
        require(pair in (b'\0\0', b'\0\x80') and tile_height(project, ctxs[t], *t) is None,
                f'Tile {t[0]},{t[1]} is not solid rock/void ({pair.hex()}); carve rooms into empty cave space',
                'UNSUPPORTED_TERRAIN')
    ground = cg.FLOOR_Y
    floor_pair = cg.PAIRS['floor'] if spec['encounters'] else cg.PAIRS['quiet_floor']
    pairs = {t: floor_pair for t in lay['floor']}
    pairs.update({t: cg.PAIRS['wall'] for t in lay['zone']})
    for x, z in lay['exits']:
        pairs[(x, z)] = cg.PAIRS['exit']
        pairs[(x, z + 1)] = cg.PAIRS['hole']
    report = {'family': cg.FAMILY, 'action': spec['action'], 'ground_height': ground,
              'cells': sorted([list(c) for c in cells]), 'footprint_tiles': len(lay['footprint']),
              'floor_tiles': len(lay['floor']), 'wall_tiles': len(lay['zone']),
              'encounters': 'cave floor 0x08 (encounter flag)' if spec['encounters'] else 'floor 0x00 (no encounters)',
              'convex_corners': len(lay['corners']), 'concave_corners': len(lay['concave']),
              'exits': [{'tile': [x, z], 'behavior': '0x6F warp south', 'hole': [x, z + 1]} for x, z in lay['exits']],
              'connection': 'link each exit with a world connection (cave exit 0x6F) to make the rooms reachable'}
    return {'cells': cells, 'ctxs': ctxs, 'pairs': pairs, 'ground': ground, 'report': report,
            'area': area_key, 'layout': lay}


def require_free(state, header, tiles, what):
    """Refuse other editors touching tiles a terrace/pond owns (rims stay impassable,
    stairs and shores keep their geometry). No-op for histories without features."""
    if not state.get('terrain_features'):
        return
    owned = {}
    for items in state['terrain_features'].values():
        for item in items:
            if item['header'] == header:
                for t in claimed(item['spec']):
                    owned.setdefault(t, item['id'])
    hit = sorted(t for t in tiles if t in owned)
    if hit:
        raise EditorError('TERRAIN_OWNED', f'{what} at {hit[0][0]},{hit[0][1]} would change terrain feature '
                          f'{owned[hit[0]]}; edit or undo that feature instead')


def feature_id(spec):
    if spec['action'] in SHAPES:
        x, z = min(tg.rect_tiles(spec['rects']))
        return f"{spec['action']}@{x},{z}"
    return f"{spec['action']}@{spec['x']},{spec['z']}"


def plan_cell(project, context, state, index, spec, ground, report):
    """One cell's terrain transaction (model and height table)."""
    from . import surface_authoring
    member = context['map_member']
    feature = {'id': feature_id(spec), 'header': context['header']['id'], 'spec': spec, 'ground': ground}
    kept = [f for f in features(state, member) if not spec.get('replaces') or f['id'] != spec['replaces']]
    trial = {**state, 'terrain_features': {**state.get('terrain_features', {}), member: kept + [feature]}}
    old_model = surface_authoring.model(project, context, state)
    try:
        new_model = surface_authoring.model(project, context, trial)
    except EditorError as exc:
        raise EditorError(exc.code if exc.code != 'INVALID_DATA' else 'UNSUPPORTED_TERRAIN', str(exc)) from exc
    budget = surface_authoring.BUDGET
    (v0, p0), (v1, p1) = surface_authoring.counts(old_model), surface_authoring.counts(new_model)
    require(p1 <= max(budget['polygons'], p0) + 0 and v1 <= max(budget['vertices'], v0),
            f'Edited model needs {p1} polygons/{v1} vertices; the measured budget is '
            f"{budget['polygons']}/{budget['vertices']}", 'RESOURCE_CAPACITY')
    old_bdhc = bdhc_bytes(project, member)
    new_bdhc = compose_bdhc(project, context, old_bdhc, spec, ground)
    deps = authoring.dependencies(context, index)
    return {'schema': SHAPE_SCHEMA if spec['action'] in SHAPES else SCHEMA, 'index': index,
            'context': authoring.context_ref(context),
            'label': spec.get('label') or ('Terrace' if is_terrace(spec) else 'Pond'),
            'request': {k: v for k, v in spec.items()}, 'family': FAMILY, 'feature': feature['id'],
            'ground': ground, 'report': report,
            'model_before_sha256': digest(old_model), 'model_after_sha256': digest(new_model),
            'model_counts': {'before': [v0, p0], 'after': [v1, p1]},
            'bdhc_before_sha256': digest(old_bdhc), 'bdhc_after_sha256': digest(new_bdhc),
            'bdhc_bytes': [len(old_bdhc), len(new_bdhc)],
            'dependencies': deps, 'dependencies_sha256': authoring.canonical(deps)}


def permission_cells(project, state, context, plan):
    cells = []
    ox, oz = context['origin']
    for t, after in sorted(plan['pairs'].items()):
        if not (ox <= t[0] < ox + 32 and oz <= t[1] < oz + 32):
            continue
        pair, _ = pair_at(project, state, context, *t)
        if pair != after:
            cells.append({'x': t[0], 'z': t[1], 'before': pair.hex(), 'after': after.hex()})
    return cells


def register(project, state, context, spec, ground):
    member = context['map_member']
    feature = {'id': feature_id(spec), 'header': context['header']['id'], 'spec': spec, 'ground': ground}
    before = bdhc_bytes(project, member)
    after = compose_bdhc(project, context, before, spec, ground)
    items = state.setdefault('terrain_features', {}).setdefault(member, [])
    if spec.get('replaces'):
        items[:] = [f for f in items if f['id'] != spec['replaces']]
    items.append(feature)
    registry = getattr(project, '_terrain_bdhc', None)
    if registry is None:
        project._terrain_bdhc = registry = {}
    registry[member] = after
    return after


def replay(project, state, t, index):
    if t.get('schema') == AMBIENT_SCHEMA:
        return replay_ambient(project, state, t, index)
    try:
        ctx = project.context(header=t['context']['header'], cell=t['context']['cell'])
        spec = normalise(t['request'], fresh=False)
        require(spec == t['request'], 'Terrain request is not canonical', 'BEFORE_VALUE_MISMATCH')
        # The global validation (ground, footprint, reachability) is recomputed from the state
        # before the feature's first cell; later cells of the same feature must repeat it.
        reports = state.setdefault('terrain_reports', {})
        key = f"{t['context']['header']}:{feature_id(spec)}:{json.dumps(spec, sort_keys=True)}"
        if key not in reports:
            fresh = plan_feature(project, state, t['context']['header'], spec)
            reports[key] = {'report': fresh['report'], 'ground': fresh['ground']}
        require(reports[key] == {'report': t['report'], 'ground': t['ground']},
                'Terrain feature validation differs from its stored report', 'BEFORE_VALUE_MISMATCH')
        expected = plan_cell(project, ctx, state, index, spec, t['ground'], t['report'])
        require(t == expected, 'Terrain before-value or dependency differs', 'BEFORE_VALUE_MISMATCH')
        register(project, state, ctx, spec, t['ground'])
        state['contexts'].append(ctx)
    except (KeyError, TypeError, ValueError) as exc:
        raise EditorError('INVALID_INPUT', 'Malformed terrain transaction') from exc


# ---- ambient sound plates (WORLD-AUDIO-001) -------------------------------------------------------

def plates_of(project, state, member):
    """Current 8-byte sound plates of a map member (composed)."""
    if member in state.get('ambient', {}):
        return [bytes.fromhex(v) for v in state['ambient'][member]]
    raw = project.member_raw(member)
    size = struct.unpack_from('<H', raw, 18)[0]
    require(size % 8 == 0, 'Unsupported sound-plate table', 'UNSUPPORTED_AMBIENT')
    return [raw[20 + i:28 + i] for i in range(0, size, 8)]


def describe_plate(i, plate, origin, weather):
    sound, volume, _, _, x0, z0, x1, z1 = plate
    name = PLATE_SOUNDS[sound] if sound < len(PLATE_SOUNDS) else f'unknown {sound}'
    conflict = weather in RAIN_WEATHER and sound < len(PLATE_SEQ)
    return {'index': i, 'sound': name, 'se': PLATE_SEQ[sound] if sound < len(PLATE_SEQ) else None,
            'volume_index': volume, 'global': [origin[0] + x0, origin[1] + z0, x1 - x0 + 1, z1 - z0 + 1],
            'raw': plate.hex(),
            'rain_conflict': conflict and ('replaces the rain loop on SDAT player 5 (PLAYER_SE_3) '
                                           'while the player stands on it')}


def plan_ambient(project, context, state, index, request):
    member = context['map_member']
    from . import world_authoring
    require(world_authoring.created(project, context), 'Ambient edits apply to project-created areas',
            'UNSUPPORTED_AMBIENT')
    current = plates_of(project, state, member)
    require(all(i < len(current) for i in request['remove']),
            f'This cell has {len(current)} sound plate(s)', 'UNSUPPORTED_AMBIENT')
    after = [p for i, p in enumerate(current) if i not in request['remove']]
    weather = context['header']['weather']
    deps = authoring.dependencies(context, index)
    return {'schema': AMBIENT_SCHEMA, 'index': index, 'context': authoring.context_ref(context),
            'label': request.get('label') or 'Remove sound plates', 'request': dict(request),
            'weather': weather,
            'removed': [describe_plate(i, current[i], context['origin'], weather) for i in request['remove']],
            'before': [p.hex() for p in current], 'after': [p.hex() for p in after],
            'dependencies': deps, 'dependencies_sha256': authoring.canonical(deps)}


def replay_ambient(project, state, t, index):
    try:
        ctx = project.context(header=t['context']['header'], cell=t['context']['cell'])
        expected = plan_ambient(project, ctx, state, index, normalise(t['request']))
        require(t == expected, 'Ambient before-value or dependency differs', 'BEFORE_VALUE_MISMATCH')
        state.setdefault('ambient', {})[ctx['map_member']] = list(t['after'])
        state['contexts'].append(ctx)
    except (KeyError, TypeError, ValueError) as exc:
        raise EditorError('INVALID_INPUT', 'Malformed ambient transaction') from exc


# ---- export -----------------------------------------------------------------------------------

def finalize(project, member, result, state):
    """Replace the BGS block and height table of an assembled member (permissions were
    written first at their stock offsets). Unchanged members are returned untouched."""
    has_bdhc = member in getattr(project, '_terrain_bdhc', {})
    has_bgs = member in state.get('ambient', {})
    if not has_bdhc and not has_bgs:
        return bytes(result)
    sec = map_sections(bytes(result))
    body = bytes(result[sec['permissions_offset']:sec['terrain_offset']])
    bgs = bytes(result[20:sec['permissions_offset']])
    if has_bgs:
        bgs = b''.join(bytes.fromhex(v) for v in state['ambient'][member])
    bdhc = project._terrain_bdhc[member] if has_bdhc else bytes(result[sec['terrain_offset']:])
    out = bytearray(result[:16]) + struct.pack('<HH', 0x1234, len(bgs)) + bgs + body + bdhc
    struct.pack_into('<I', out, 12, len(bdhc))
    check = map_sections(bytes(out))
    require(bytes(out[check['permissions_offset']:check['terrain_offset']]) == body
            and bytes(out[check['terrain_offset']:]) == bdhc, 'Terrain export readback differs')
    return bytes(out)


# ---- report -------------------------------------------------------------------------------

def summary(t):
    if t['schema'] == AMBIENT_SCHEMA:
        return {'operation': 'terrain.ambient', 'index': t['index'], 'label': t['label'], 'context': t['context'],
                'removed': [{k: p[k] for k in ('index', 'sound', 'global')} for p in t['removed']],
                'plates': [len(t['before']), len(t['after'])]}
    return {'operation': 'terrain.feature', 'index': t['index'], 'label': t['label'], 'context': t['context'],
            'feature': t['feature'], 'family': t['family'], 'request': t['request'], 'report': t['report'],
            'model_counts': t['model_counts'], 'bdhc_bytes': t['bdhc_bytes']}


def view(project, header):
    """Inspect: supported family, per-cell materials/features/heights and sound plates."""
    state = project.composed()
    key, area = area_of(project, state, header)
    head = project.header(header)
    rows = []
    for cell in area['cells']:
        ctx = project.context(header=header, cell=cell['cell'])
        from . import surface_authoring, mapscene, nitro
        model = surface_authoring.model(project, ctx, state)
        _, blobs = mapscene.tilesets(project, ctx)
        _, prims = nitro.decode_model(model, tileset=blobs['map_tileset'], render=False)
        names = {p.material['texture_name'] for p in prims}
        plates = plates_of(project, state, ctx['map_member'])
        rows.append({'cell': cell['cell'], 'map_member': ctx['map_member'], 'origin': ctx['origin'],
                     'terrace_materials': all(m in names for m in tg.TERRACE_MATERIALS),
                     'pond_materials': all(m in names for m in tg.WATER_MATERIALS),
                     'features': [f['id'] for f in features(state, ctx['map_member'])],
                     'sound_plates': [describe_plate(i, p, ctx['origin'], head['weather']) for i, p in enumerate(plates)]})
    conflicts = sum(1 for r in rows for p in r['sound_plates'] if p['rain_conflict'])
    return {'revision': project.doc['revision'], 'area': key, 'header': header, 'family': FAMILY,
            'weather': head['weather'], 'rain_plate_conflicts': conflicts,
            'features': [{'id': f['id'], 'spec': f['spec'], 'ground': f['ground']}
                         for f in area_features(project, state, header)],
            'cells': [r for r in rows if r['terrace_materials'] or r['pond_materials'] or r['features']
                      or r['sound_plates']],
            'other_cells': sum(1 for r in rows if not (r['terrace_materials'] or r['pond_materials'] or r['features']
                                                       or r['sound_plates'])),
            'limits': {'terrace_top_side': list(TOP_SIDE), 'pond_side': list(POND_SIDE), 'stairs': MAX_ACCESS,
                       'stair_width': tg.STAIR_WIDTH, 'rise_model_units': tg.RISE, 'water_drop': tg.WATER_DROP,
                       'cells_per_feature': 4,
                       'shapes': {'rects': SHAPE_RECTS, 'span': SHAPE_SPAN, 'stairs': SHAPE_ACCESS,
                                  'ledges': SHAPE_LEDGES, 'ledge_length': LEDGE_LENGTH, 'levels': [1, 2],
                                  'min_water_width': 2}},
            'notes': ['Terraces rise one stock step (16 model units = 1.0 BDHC) above one flat ground height; '
                      'rims block, stairs (00 06) connect; see docs/TERRAIN_AUTHORING.md.',
                      'Ponds are stock sea surface and shore in cells whose model holds the sea materials; '
                      'traversable ponds are Surf water (15 00), decorative ponds are blocked.',
                      'Sound plates sharing PLAYER_SE_3 with rain stop the rain loop (WORLD-AUDIO-001).']}
