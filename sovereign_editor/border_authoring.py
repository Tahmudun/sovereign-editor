"""Bordered path and tall-grass patches: surface transactions v5 (PROD-VIS-001).

One request (family + global tiles) is planned once per touched cell, so a patch
crossing a seam gets one surface transaction per cell, each deriving its part of
the same globally computed stock pieces (borders.py). Paths repaint ground tiles
(``road01`` inside, oriented ``road01_r`` rims around); tall grass adds overlay
quads (``egrass`` inside, egrass_u/v/ro/ri around) two units above ground and two
units south, like every stock patch, clipped at cell bounds. Behaviors are
separate permission transactions. Pure planning; Project owns writes.
"""
import copy

import numpy as np

from . import authoring, borders, mapscene, nitro, scenery, world
from .formats import EditorError, digest, map_data, require

SCHEMA = 'sovereign-surface-transaction-v5'
FAMILIES = ('path', 'tall_grass')
MAX_TILES = 256
MAX_PIECES = 512                     # overlay pieces per map resource
# Authored surface tiles per map resource for bordered paths: a whole cell (original content
# v1, a paved town). The binding guards stay the stock-p90 polygon/vertex budget and the
# 60 KiB map buffer, both checked on the rebuilt model.
MAX_SURFACE_TILES = 1024
SHIFT = borders.GRASS_SHIFT / 16     # tiles


ROAD = ('road01', 'road01_r', 'road01_sub')


def tile_set(tiles, allow_empty=False):
    require(isinstance(tiles, list) and (0 if allow_empty else 1) <= len(tiles) <= MAX_TILES and all(
        isinstance(t, dict) and set(t) == {'x', 'z'} and type(t['x']) is int and type(t['z']) is int for t in tiles),
        f'Choose 1..{MAX_TILES} tiles with integer x/z', 'INVALID_INPUT')
    coords = {(t['x'], t['z']) for t in tiles}
    require(len(coords) == len(tiles), 'Tiles must be distinct', 'INVALID_INPUT')
    return coords


def cell_of(project, header, x, z):
    """The area cell owning a global tile, or a refusal (the ring may not leave the area)."""
    head = project.header(header)
    grid = project.matrix_data(head['matrix'])
    cx, cy = x // 32, z // 32
    require(x >= 0 and z >= 0 and cx < grid['width'] and cy < grid['height'] and grid['maps'][cy][cx] != world.EMPTY
            and (not grid['has_headers'] or grid['headers'][cy][cx] == header),
            f'Tile {x},{z} (or its border) lies outside this area', 'UNSUPPORTED_BORDER')
    return cx, cy


def shapes(project, context, state):
    """texture name -> (material name, flat heights) of one cell's current model."""
    from . import surface_authoring
    cache = project.__dict__.setdefault('_border_shape_cache', {})
    raw = surface_authoring.model(project, context, state)
    key = (context['map_member'], digest(raw))
    if key not in cache:
        _, blobs = mapscene.tilesets(project, context, state)
        _, prims = nitro.decode_model(raw, tileset=blobs['map_tileset'], render=False)
        result = {}
        for p in prims:
            heights = sorted({round(float(p.vertices[t[0], 1]), 4) for t in p.triangles if np.ptp(p.vertices[t, 1]) < 1e-4})
            result.setdefault(p.material['texture_name'], (p.material['name'], heights))
        cache[key] = result
    return cache[key]


def window_set(project, header, window):
    require(isinstance(window, dict) and set(window) == {'x', 'z', 'width', 'height'}
            and all(type(window[k]) is int for k in window) and 1 <= window['width'] <= 64 and 1 <= window['height'] <= 64,
            'A border window is an integer rectangle up to 64 by 64 tiles', 'INVALID_INPUT')
    tiles = {(x, z) for x in range(window['x'], window['x'] + window['width'])
             for z in range(window['z'], window['z'] + window['height'])}
    for x, z in tiles:
        cell_of(project, header, x, z)
    return tiles


def top_material(project, header, state, x, z, memo=None):
    """Texture of the highest ground surface (grass overlays ignored) at a tile centre.

    `memo` (a dict owned by one caller, for one unchanging state) keeps each cell's
    grid, so a region query composes each cell model once instead of once per tile."""
    cx, cy = cell_of(project, header, x, z)
    if memo is not None:
        if (cx, cy) not in memo:
            memo[(cx, cy)] = _top_grid(project, header, state, cx, cy)
        origin, names = memo[(cx, cy)]
    else:
        origin, names = _top_grid(project, header, state, cx, cy)
    return names[z - origin[1], x - origin[0]]


def _top_grid(project, header, state, cx, cy):
    from . import surface_authoring
    context = project.context(header=header, cell=[cx, cy])
    raw = surface_authoring.model(project, context, state)
    cache = project.__dict__.setdefault('_border_top_cache', {})
    key = (context['map_member'], digest(raw))
    if key not in cache:
        _, blobs = mapscene.tilesets(project, context, state)
        _, prims = nitro.decode_model(raw, tileset=blobs['map_tileset'], render=False)
        best = np.full((32, 32), -1e9)
        names = np.empty((32, 32), dtype=object)
        for prim in prims:
            if prim.material['texture_name'] in borders.GRASS_FAMILY:
                continue              # transparent grass overlays: judge the ground beneath
            if not len(prim.triangles):
                continue
            # Every (triangle, candidate tile) system is solved in one stacked call:
            # the same single-right-hand-side solve per tile as before, in the same
            # order, followed by the same sequential highest-surface update.
            verts = prim.vertices[prim.triangles]
            xs, zs = (verts[:, :, 0] + 256) / 16 - 0.5, (verts[:, :, 2] + 256) / 16 - 0.5
            systems = np.stack([xs, zs, np.ones_like(xs)], axis=2)
            usable = np.abs(np.linalg.det(systems)) >= 1e-9
            x0 = np.maximum(0, np.floor(xs.min(1)).astype(int)); x1 = np.minimum(32, np.ceil(xs.max(1)).astype(int) + 1)
            z0 = np.maximum(0, np.floor(zs.min(1)).astype(int)); z1 = np.minimum(32, np.ceil(zs.max(1)).astype(int) + 1)
            pairs = [(t, tx, tz) for t in np.nonzero(usable)[0].tolist()
                     for tx in range(int(x0[t]), int(x1[t])) for tz in range(int(z0[t]), int(z1[t]))]
            if not pairs:
                continue
            index = np.array(pairs)
            rhs = np.stack([index[:, 1].astype(float), index[:, 2].astype(float), np.ones(len(index))], axis=1)[:, :, None]
            weights = np.linalg.solve(systems[index[:, 0]].transpose(0, 2, 1), rhs)[:, :, 0]
            inside = weights.min(1) >= -1e-6
            name = prim.material['texture_name']
            for k in np.nonzero(inside)[0].tolist():
                t, tx, tz = pairs[k]
                h = float(weights[k] @ verts[t][:, 1])
                if h > best[tz, tx] + 1e-4:
                    best[tz, tx], names[tz, tx] = h, name
        cache[key] = names
    return context['origin'], cache[key]


def ground_height(project, context, x, z):
    return scenery.floor_height(project, context, {'x': x + 0.5, 'z': z + 0.5}) * 16


def custom_region(project, header, state, material):
    """Global tiles of an area currently painted with a ground material's fill / rims."""
    from . import ground_materials as gm
    fill = gm.texture_name(material, 'fill')
    rims = {gm.texture_name(material, f'rim:{k}') for k in gm.RIM_ROLES}
    area_cells = {}
    for ctx in (state or {}).get('contexts', []):
        if ctx['header']['id'] == header:
            area_cells[ctx['map_member']] = ctx['origin']
    filled, rimmed = set(), {}
    for member, origin in area_cells.items():
        for (lx, lz), v in (state.get('surfaces') or {}).get(member, {}).items():
            if v.get('material') == fill:
                filled.add((origin[0] + lx, origin[1] + lz))
            elif v.get('material') in rims:
                rimmed[(origin[0] + lx, origin[1] + lz)] = v['material']
    return filled, rimmed


def terrace_tiles(state, header):
    """(tops, landings, edges) of this area's terraces. Ground-material paving may cover a
    top or a stair landing (texture only; behavior stays walkable); edges (rims, stairs,
    ledges) belong to the terrace."""
    tops, landings, edges = set(), set(), set()
    if not state or not state.get('terrain_features'):
        return tops, landings, edges
    from . import terrain_authoring
    for items in state['terrain_features'].values():
        for item in items:
            if item['header'] == header and terrain_authoring.is_terrace(item['spec']):
                lay = terrain_authoring.layout(item['spec'])
                tops |= set(lay['top'])
                landings |= set(lay['landings'])
                edges |= set(lay['footprint']) - set(lay['top'])
    return tops, landings, edges - landings


def custom_pieces(project, header, material, tiles, state, erase=False):
    """Cobble-style paving: fill over the union of the request and the material's existing
    fill in this area, a stock-layout rim ring around that union (dirt rim over stock dirt,
    grass rim elsewhere) and stock grass back on rims the new union no longer needs, so a
    repaint never leaves an old border behind (SURFACE-02)."""
    from . import ground_materials as gm
    entry = gm.materials(state).get(material)
    require(entry is not None, f'Ground material {material} is not registered', 'NOT_FOUND')
    roles = set(gm.verify_package(project, material, entry['package'])['roles'])
    require('fill' in roles and 'rim:grass' in roles, f'Ground material {material} has no fill and grass rim to pave with',
            'UNSUPPORTED_BORDER')
    fill = gm.texture_name(material, 'fill')
    filled, rimmed = custom_region(project, header, state, material)
    region = (filled - tiles) if erase else (filled | tiles)
    # A terrace's cliff rim, stairs and ledges already edge the paving (a raised street, or a
    # lower street along a cliff), so the ring stops there instead of repainting the terrace.
    edges = terrace_tiles(state, header)[2]
    ring = [r for r in borders.rim_pieces(region, allow_unsupported=True) if r['tile'] not in edges] if region else []
    bad = [r for r in ring if r['kind'] == 'unsupported']
    require(not bad, f"Tile {bad[0]['tile'][0]},{bad[0]['tile'][1]} lies between two paved parts "
            f"({bad[0]['orientation']}); widen, join or separate them" if bad else '', 'UNSUPPORTED_BORDER')
    memo, paint = {}, {}
    for t in sorted(region - filled):
        paint[t] = (fill, None, None, 'replace')
    ring_tiles = {r['tile'] for r in ring}
    for r in ring:
        t = r['tile']
        if t in rimmed:
            role = 'dirt' if rimmed[t] == gm.texture_name(material, 'rim:dirt') else 'grass'
        else:
            try:
                under = top_material(project, header, state, *t, memo=memo)
            except EditorError:
                under = None
            role = 'dirt' if under in ROAD and 'rim:dirt' in roles else 'grass'
        name = gm.texture_name(material, f'rim:{role}')
        if rimmed.get(t) != name or t in tiles:
            paint[t] = (name, r['kind'], r['orientation'], 'replace')
        elif rimmed.get(t) == name:
            paint[t] = (name, r['kind'], r['orientation'], 'replace')
    for t, name in rimmed.items():
        if t not in ring_tiles and t not in region:
            paint[t] = ('road01' if name == gm.texture_name(material, 'rim:dirt') else 'grass01gs', None, None, 'replace')
    if erase:
        for t in tiles & filled:
            if t not in ring_tiles:
                paint[t] = ('grass01gs', None, None, 'replace')
    per_cell = {}
    for (x, z), value in sorted(paint.items()):
        per_cell.setdefault(cell_of(project, header, x, z), {'paint': {}, 'overlay': []})['paint'][(x, z)] = value
    return per_cell


def pieces(project, header, family, tiles, window=None, state=None, material=None, erase=False):
    """Global plan: {cell: {'paint': {(x,z): (texture, kind, orientation[, mode])}, 'overlay': [...]}}."""
    require(family in FAMILIES, 'Border family is path or tall_grass', 'INVALID_INPUT')
    if material is not None:
        require(family == 'path' and window is None, 'A ground material paints paths from listed tiles', 'INVALID_INPUT')
        return custom_pieces(project, header, material, tiles, state, erase)
    require(not erase, 'Erase applies to a ground-material path', 'INVALID_INPUT')
    per_cell = {}
    if window is not None:
        require(family == 'path', 'A window reroutes paths; tall grass uses listed tiles', 'INVALID_INPUT')
        area = window_set(project, header, window)
        require(tiles <= area, 'New path tiles must lie inside the window', 'INVALID_INPUT')
        # Existing road just outside joins the region, so cut ends get stock caps
        # and junction corners; rims are laid only inside the window.
        outside = {(x + dx, z + dz) for x, z in area for dx in (-1, 0, 1) for dz in (-1, 0, 1)} - area
        keep, memo = set(), {}
        for x, z in outside:
            try:
                if top_material(project, header, state, x, z, memo) in ('road01', 'road01_sub'):
                    keep.add((x, z))
            except EditorError:
                continue
        region = tiles | keep
        ring = [r for r in borders.rim_pieces(region, allow_unsupported=True) if r['tile'] in area] if region else []
        bad = [r for r in ring if r['kind'] == 'unsupported']
        require(not bad, f"Tile {bad[0]['tile'][0]},{bad[0]['tile'][1]} lies between two path segments "
                f"({bad[0]['orientation']}); widen, join or separate them" if bad else '', 'UNSUPPORTED_BORDER')
        paint = {t: ('road01', None, None, 'replace') for t in tiles}
        paint.update({r['tile']: ('road01_r', r['kind'], r['orientation'], 'replace') for r in ring})
        for t in sorted(area - set(paint)):
            if top_material(project, header, state, *t, memo=memo) in ROAD:
                paint[t] = ('grass01gs', None, None, 'replace')
        for (x, z), value in sorted(paint.items()):
            per_cell.setdefault(cell_of(project, header, x, z), {'paint': {}, 'overlay': []})['paint'][(x, z)] = value
        return per_cell
    if family == 'path':
        for x, z in sorted(tiles):
            per_cell.setdefault(cell_of(project, header, x, z), {'paint': {}, 'overlay': []})['paint'][(x, z)] = ('road01', None, None)
        for rim in borders.rim_pieces(tiles):
            x, z = rim['tile']
            per_cell.setdefault(cell_of(project, header, x, z), {'paint': {}, 'overlay': []})['paint'][(x, z)] = (
                'road01_r', rim['kind'], rim['orientation'])
        return per_cell
    interior, ring = borders.grass_pieces(tiles)
    quads = []
    for piece in interior:
        x, z = piece['tile']
        quads.append(('egrass', (x, z + SHIFT, x + 1, z + 1 + SHIFT), 'tile', (x, z)))
    for piece in ring:
        hx, hz = piece['half']
        quads.append((piece['material'], (hx / 2, hz / 2 + SHIFT, (hx + 1) / 2, (hz + 1) / 2 + SHIFT), 'half', (hx, hz)))
    for material, rect, kind, index in quads:
        # Every tile the quad touches must be in the area; pieces are clipped per cell.
        x0, z0, x1, z1 = rect
        touched = {(int(np.floor(x)), int(np.floor(z))) for x in (x0, x1 - 1e-6) for z in (z0, z1 - 1e-6)}
        for tx, tz in touched:
            cell = cell_of(project, header, tx, tz)
            per_cell.setdefault(cell, {'paint': {}, 'overlay': []})
        owner = cell_of(project, header, int(np.floor(x0)), int(np.floor(z0 + 1e-6 - SHIFT)))
        for cell in sorted({cell_of(project, header, tx, tz) for tx, tz in touched}):
            per_cell[cell]['overlay'].append((material, rect, kind, index, owner))
    return per_cell


def _uv(material, kind, index, owner, rect):
    """UV at the unclipped rect corners, in the owning cell's coordinates."""
    ox, oz = owner[0] * 32, owner[1] * 32
    if kind == 'tile':
        x, z = index[0] - ox, index[1] - oz
        return np.array([(x, z), (x + 1, z), (x + 1, z + 1), (x, z + 1)], dtype=float)
    hx, hz = index[0] - 2 * ox, index[1] - 2 * oz
    return np.array(borders.half_uv(material, hx, hz), dtype=float)


def _clip(rect, uv, bounds):
    """Clip an axis-aligned quad to a cell (tile units); UVs interpolate linearly."""
    x0, z0, x1, z1 = rect
    bx0, bz0, bx1, bz1 = bounds
    cx0, cz0, cx1, cz1 = max(x0, bx0), max(z0, bz0), min(x1, bx1), min(z1, bz1)
    if cx1 - cx0 < 1e-6 or cz1 - cz0 < 1e-6:
        return None

    def at(x, z):
        s, t = (x - x0) / (x1 - x0), (z - z0) / (z1 - z0)
        top = uv[0] * (1 - s) + uv[1] * s
        bottom = uv[3] * (1 - s) + uv[2] * s
        return top * (1 - t) + bottom * t
    return (cx0, cz0, cx1, cz1), [at(cx0, cz0), at(cx1, cz0), at(cx1, cz1), at(cx0, cz1)]


BEHAVIORS = ('grass', 'ground')


def behavior_of(family, behavior=None):
    """Tile behavior a border gives its interior: paths walk (ground); tall grass is encounter grass
    unless ``behavior`` is 'ground' (decorative grass: the stock look on ordinary walkable ground)."""
    require(behavior is None or family == 'tall_grass' and behavior in BEHAVIORS,
            'behavior (grass or ground) applies to tall grass', 'INVALID_INPUT')
    return 'path' if family == 'path' or behavior == 'ground' else 'grass'


def plan(project, context, state, index, family, tiles, label=None, window=None, material=None, erase=False,
         behavior=None):
    """The part of one border request that falls in ``context``'s cell."""
    from . import surface_authoring
    require(label is None or isinstance(label, str) and 0 < len(label) <= 160, 'Invalid border label', 'INVALID_INPUT')
    require(type(erase) is bool and (material is None or isinstance(material, str)), 'Invalid material/erase', 'INVALID_INPUT')
    coords = tile_set(tiles, allow_empty=window is not None)
    header = context['header']['id']
    if behavior_of(family, behavior) == 'grass':
        require(context['header']['wild_pokemon'] != 255, 'Tall grass needs an area with an encounter table '
                '(or behavior ground: decorative grass without encounters)', 'NO_ENCOUNTERS')
    plan_cells = pieces(project, header, family, coords, window, state, material, erase)
    cell = (context['cell']['x'], context['cell']['y'])
    require(cell in plan_cells, 'This border does not touch the chosen cell', 'INVALID_INPUT')
    part = plan_cells[cell]
    if state.get('terrain_features'):
        from . import terrain_authoring
        touched = set(part['paint']) | {(int(np.floor(q[1][0])), int(np.floor(q[1][1]))) for q in part['overlay']}
        if material is not None:
            # Ground-material paint composes after terrain, so it may pave a terrace's flat top
            # and the landings in front of its stairs (never its rim, stairs or ledges).
            tops, landings, _ = terrace_tiles(state, header)
            touched -= tops | landings
        terrain_authoring.require_free(state, header, touched, 'Border')
    ox, oz = context['origin']
    member = context['map_member']
    current = copy.deepcopy(state.get('surfaces', {}).get(member, {}))
    before_cells = copy.deepcopy(current)
    overlay = copy.deepcopy(state.get('surface_pieces', {}).get(member, {}))
    before_pieces = copy.deepcopy(overlay)
    found = shapes(project, context, state)
    from . import ground_materials as gm
    sampler = None
    for (x, z), (texture, kind, orientation, *mode) in sorted(part['paint'].items()):
        if gm.is_custom(texture):
            if sampler is None:
                from . import surface_native
                stock_cells = {k: v for k, v in current.items() if not gm.is_custom(v.get('material'))}
                stock = {**state, 'surfaces': {**state.get('surfaces', {}), member: stock_cells}}
                _, blobs = mapscene.tilesets(project, context, state)
                ground_model = surface_authoring.model(project, context, stock)
                _, prims = nitro.decode_model(ground_model, tileset=blobs['map_tileset'], render=False)
                # Judge the ground itself: stock decals (tufts, flowers, pebbles) and grass
                # overlays sit above it and are replaced or kept by the painter.
                skip = set(surface_native.DECAL_TEXTURES) | set(surface_native.PIECE_TEXTURES)
                sampler = surface_native.mapping_sampler(ground_model, blobs['map_tileset'],
                                                         [q for q in prims if (q.material['texture_name'] or '') not in skip])
            height = sampler(x - ox, z - oz)['height']
            entry = {'material': texture, 'height': height, 'mode': 'replace'}
            if kind is not None:
                entry['affine'] = [round(v, 12) for v in borders.rim_affine(kind, orientation, (x - ox, z - oz), (ox, oz)).ravel()]
            else:
                width, depth = next((w, h) for n, w, h in gm.custom_for(project, state, context) if n == texture)
                # One texel per model unit, phased on global coordinates (repeat: only the
                # fractional offset matters, which keeps TEXCOORD within its 16-bit range).
                entry['affine'] = [round(v, 12) for v in (1 / width, 0.0, 0.0, 1 / depth,
                                                          ((256 + ox * 16) % width) / width,
                                                          ((256 + oz * 16) % depth) / depth)]
            current[(x - ox, z - oz)] = entry
            continue
        require(texture in found, f'Cell {cell} has no stock {texture} surface; choose donors with the path family',
                'UNSUPPORTED_BORDER')
        stock_material, heights = found[texture]
        ground = ground_height(project, context, x, z)
        near = [h for h in heights if abs(h - ground) <= 2]
        require(near, f'Tile {x},{z}: no {texture} plane at its floor height', 'UNSUPPORTED_HEIGHT')
        height = min(near, key=lambda h: (abs(h - ground), h))
        entry = {'material': stock_material, 'height': height}
        if mode and mode[0]:
            entry['mode'] = mode[0]
        if kind is not None:
            entry['affine'] = [round(v, 12) for v in borders.rim_affine(kind, orientation, (x - ox, z - oz), (ox, oz)).ravel()]
        current[(x - ox, z - oz)] = entry
    for piece_material, rect, kind, index_, owner in part['overlay']:
        require(piece_material in found, f'Cell {cell} has no stock {piece_material} piece; choose donors with the tall-grass family',
                'UNSUPPORTED_BORDER')
        uv = _uv(piece_material, kind, index_, owner, rect)
        clipped = _clip(rect, uv, (ox, oz, ox + 32, oz + 32))
        if clipped is None:
            continue
        (x0, z0, x1, z1), corners = clipped
        tx, tz = (int(np.floor(index_[0] / (2 if kind == 'half' else 1))),
                  int(np.floor(index_[1] / (2 if kind == 'half' else 1))))
        ground = ground_height(project, context, min(max(tx, ox), ox + 31), min(max(tz, oz), oz + 31))
        model = [round((v - o) * 16 - 256, 6) for v, o in ((x0, ox), (z0, oz), (x1, ox), (z1, oz))]
        key = f'{piece_material}:{model[0]:g}:{model[1]:g}'
        require(key not in overlay, f'Tall grass border already at {x0:g},{z0:g}', 'UNSUPPORTED_BORDER')
        overlay[key] = {'material': piece_material, 'rect': model, 'height': ground + borders.GRASS_RISE,
                        'uv': [round(float(v), 6) for c in corners for v in c]}
    require(len(current) <= MAX_SURFACE_TILES, f'At most {MAX_SURFACE_TILES} authored surface tiles per resource',
            'RESOURCE_CAPACITY')
    require(len(overlay) <= MAX_PIECES, f'At most {MAX_PIECES} border pieces per resource', 'RESOURCE_CAPACITY')
    trial = {**state, 'surfaces': {**state.get('surfaces', {}), member: current},
             'surface_pieces': {**state.get('surface_pieces', {}), member: overlay},
             'surface_versions': {**state.get('surface_versions', {}), member: 5}}
    try:
        old, new = surface_authoring.model(project, context, state), surface_authoring.model(project, context, trial)
    except EditorError as exc:
        raise EditorError(exc.code if exc.code != 'INVALID_DATA' else 'UNSUPPORTED_BORDER', str(exc)) from exc
    if new != old:
        (v0, p0), (v1, p1) = surface_authoring.counts(old), surface_authoring.counts(new)
        budget = surface_authoring.BUDGET
        require(p1 <= max(budget['polygons'], p0) and v1 <= max(budget['vertices'], v0),
                f'Edited model needs {p1} polygons/{v1} vertices; the measured budget is '
                f"{budget['polygons']}/{budget['vertices']}. Choose fewer tiles", 'RESOURCE_CAPACITY')
    cells = [{'x': k[0], 'z': k[1], 'before': before_cells.get(k), 'after': current.get(k)}
             for k in sorted(before_cells.keys() | current.keys()) if before_cells.get(k) != current.get(k)]
    changes = [{'key': k, 'before': before_pieces.get(k), 'after': overlay.get(k)}
               for k in sorted(before_pieces.keys() | overlay.keys()) if before_pieces.get(k) != overlay.get(k)]
    deps = authoring.dependencies(context, index)
    return {'schema': SCHEMA, 'index': index, 'context': authoring.context_ref(context),
            'label': label or ('Bordered path' if family == 'path' else 'Bordered tall grass'),
            'request': {'family': family, 'tiles': [{'x': x, 'z': z} for x, z in sorted(coords)], 'label': label,
                        **({'window': dict(window)} if window is not None else {}),
                        **({'material': material} if material is not None else {}),
                        **({'erase': True} if erase else {}),
                        **({'behavior': behavior} if behavior is not None else {})},
            'cells': cells, 'pieces': changes, 'before_sha256': digest(old), 'after_sha256': digest(new),
            'writer_before': state.get('surface_versions', {}).get(member, 1),
            'dependencies': deps, 'dependencies_sha256': authoring.canonical(deps)}


def replay(project, state, t, index):
    try:
        ctx = project.context(header=t['context']['header'], cell=t['context']['cell'])
        expected = plan(project, ctx, state, index, **t['request'])
        require(t == expected and changed(t), 'Border before-value or dependency differs', 'BEFORE_VALUE_MISMATCH')
        member = ctx['map_member']
        cells = state.setdefault('surfaces', {}).setdefault(member, {})
        for c in t['cells']:
            if c['after'] is None:
                cells.pop((c['x'], c['z']), None)
            else:
                cells[(c['x'], c['z'])] = c['after']
        overlay = state.setdefault('surface_pieces', {}).setdefault(member, {})
        for c in t['pieces']:
            if c['after'] is None:
                overlay.pop(c['key'], None)
            else:
                overlay[c['key']] = c['after']
        state.setdefault('surface_versions', {})[member] = 5
        state['contexts'].append(ctx)
    except (KeyError, TypeError, ValueError) as exc:
        raise EditorError('INVALID_INPUT', 'Malformed border transaction') from exc


def changed(t):
    return bool(t['cells'] or t['pieces']) and t['before_sha256'] != t['after_sha256']


def touched_cells(project, header, family, tiles, window=None, state=None, material=None, erase=False):
    return sorted(pieces(project, header, family, tile_set(tiles, allow_empty=window is not None), window, state,
                         material, erase))


def interior_by_cell(tiles):
    result = {}
    for t in tiles:
        result.setdefault((t['x'] // 32, t['z'] // 32), []).append((t['x'], t['z']))
    return result
