"""Editable ground decals (SURFACE-03): petal piles, scatters and other ground details.

A decal is a ground-material ``decal:<key>`` texture (index 0 transparent) laid over the
map's own ground: every ground face under its footprint (flat or sloped, stock or custom
paving) is clipped to the decal rectangle and copied ``RISE`` model units up (plus
``LAYER_STEP`` per layer), textured one texel per model unit, like the stock flower and
tuft decals that sit one unit above ground. Decals add no collision and change no
permission, height or encounter behavior; they never replace the ground beneath them.

Transactions (``sovereign-decal-transaction-v1``) are per map cell and name a stable
instance key: ``place`` (material, decal, x, z, rotation, flip, layer), ``move``,
``duplicate``, ``erase`` and ``revise`` (any of material/decal/rotation/flip/layer). ``x``,
``z`` are the decal centre in global tiles (sixteenths allowed). A decal that crosses a
cell seam is placed once per touched cell (each cell keeps its clipped part).
Pure planning; Project owns writes.
"""
import copy

import numpy as np

from . import authoring
from .formats import EditorError, digest, require

SCHEMA = 'sovereign-decal-transaction-v1'
ACTIONS = ('place', 'move', 'duplicate', 'erase', 'revise')
RISE, LAYER_STEP, LAYERS = 1.0, 0.5, 4
MAX_PER_MEMBER = 64
FIELDS = ('material', 'decal', 'x', 'z', 'rotation', 'flip', 'layer')
# Stock ground faces a decal may also lie on besides the flat paintable ground (grassy ramps).
DRAPE_EXTRA = {'grass_slope'}
# Faces steeper than ~72 degrees (normal y below this) are walls; stock grassy ramps are ~61.
STEEPEST = 0.3


def is_transaction(t):
    return isinstance(t, dict) and t.get('schema') == SCHEMA


def decals(state, member):
    return (state.get('ground_decals') or {}).get(member, {})


def _spec(project, state, request, before=None):
    from . import ground_materials as gm
    spec = dict(before or {})
    for k in FIELDS:
        if k in request:
            spec[k] = request[k]
    spec.setdefault('rotation', 0)
    spec.setdefault('flip', False)
    spec.setdefault('layer', 0)
    require(set(spec) == set(FIELDS), f"A decal has {', '.join(FIELDS)}", 'INVALID_INPUT')
    entry = gm.materials(state).get(spec['material'])
    require(entry is not None, f"Ground material {spec['material']} is not registered", 'NOT_FOUND')
    roles = gm.verify_package(project, spec['material'], entry['package'])['roles']
    require(f"decal:{spec['decal']}" in roles, f"Material {spec['material']} has no decal {spec['decal']}", 'NOT_FOUND')
    require(all(isinstance(spec[k], (int, float)) and not isinstance(spec[k], bool) and float(spec[k] * 16).is_integer()
                for k in ('x', 'z')), 'Decal position is in tiles, in sixteenths', 'INVALID_INPUT')
    require(spec['rotation'] in (0, 90, 180, 270) and type(spec['flip']) is bool and type(spec['layer']) is int
            and 0 <= spec['layer'] < LAYERS, 'rotation 0/90/180/270, flip true/false, layer 0..3', 'INVALID_INPUT')
    spec['x'], spec['z'] = float(spec['x']), float(spec['z'])
    return spec


def _size(project, state, spec, context):
    from . import ground_materials as gm
    name = gm.texture_name(spec['material'], f"decal:{spec['decal']}")
    for n, w, h in gm.custom_for(project, state, context):
        if n == name:
            return name, w, h
    require(False, f'Decal texture {name} is not available in this area', 'NOT_FOUND')


def footprint(project, state, spec, context):
    """Global tile rectangle (x0, z0, x1, z1) a decal covers, rotation applied."""
    _, w, h = _size(project, state, spec, context)
    if spec['rotation'] in (90, 270):
        w, h = h, w
    return (spec['x'] - w / 32, spec['z'] - h / 32, spec['x'] + w / 32, spec['z'] + h / 32)


def plan(project, context, state, index, action, key=None, source=None, label=None, **request):
    require(action in ACTIONS, f"Decal action is one of {', '.join(ACTIONS)}", 'INVALID_INPUT')
    require(isinstance(key, str) and 0 < len(key) <= 32, 'A decal needs a stable key', 'INVALID_INPUT')
    require(label is None or isinstance(label, str) and 0 < len(label) <= 160, 'Invalid decal label', 'INVALID_INPUT')
    member = context['map_member']
    current = decals(state, member)
    before = copy.deepcopy(current.get(key))
    if action == 'place':
        require(before is None, f'Decal {key} already exists here; revise or move it', 'EXISTS')
        after = _spec(project, state, request)
    elif action == 'duplicate':
        require(before is None, f'Decal {key} already exists here', 'EXISTS')
        require(isinstance(source, str) and source in current, f'No decal {source} in this cell to duplicate', 'NOT_FOUND')
        require(set(request) <= {'x', 'z'} and request, 'A duplicate names its new x/z', 'INVALID_INPUT')
        after = _spec(project, state, request, current[source])
    else:
        require(before is not None, f'No decal {key} in this cell', 'NOT_FOUND')
        if action == 'erase':
            require(not request, 'Erase takes only the key', 'INVALID_INPUT')
            after = None
        elif action == 'move':
            require(set(request) <= {'x', 'z'} and request, 'A move names x and/or z', 'INVALID_INPUT')
            after = _spec(project, state, request, before)
        else:
            require(set(request) <= set(FIELDS) - {'x', 'z'} and request, 'Revise changes material, decal, rotation, '
                    'flip or layer', 'INVALID_INPUT')
            after = _spec(project, state, request, before)
        require(after != before, 'The decal is unchanged', 'NO_CHANGE')
    if after is not None:
        x0, z0, x1, z1 = footprint(project, state, after, context)
        ox, oz = context['origin']
        require(x1 > ox and x0 < ox + 32 and z1 > oz and z0 < oz + 32, 'The decal does not touch this cell',
                'OUTSIDE_MAP')
        if state.get('terrain_features'):
            from . import border_authoring, terrain_authoring
            touched = {(int(np.floor(x)), int(np.floor(z))) for x in np.arange(max(x0, ox), min(x1, ox + 32), 0.5)
                       for z in np.arange(max(z0, oz), min(z1, oz + 32), 0.5)}
            # A decal may lie on a terrace's flat top or a stair landing (a raised street's
            # petal pile); never across its rim, stairs or ledges.
            tops, landings, _ = border_authoring.terrace_tiles(state, context['header']['id'])
            terrain_authoring.require_free(state, context['header']['id'], touched - tops - landings, 'Decal')
    items = dict(current)
    if after is None:
        items.pop(key)
    else:
        items[key] = after
    require(len(items) <= MAX_PER_MEMBER, f'At most {MAX_PER_MEMBER} decals per map resource', 'RESOURCE_CAPACITY')
    from . import surface_authoring
    trial = {**state, 'ground_decals': {**(state.get('ground_decals') or {}), member: items}}
    old, new = surface_authoring.model(project, context, state), surface_authoring.model(project, context, trial)
    (v0, p0), (v1, p1) = surface_authoring.counts(old), surface_authoring.counts(new)
    budget = surface_authoring.BUDGET
    require(new == old or p1 <= max(budget['polygons'], p0) and v1 <= max(budget['vertices'], v0),
            f"Edited model needs {p1} polygons/{v1} vertices; the measured budget is {budget['polygons']}/"
            f"{budget['vertices']}", 'RESOURCE_CAPACITY')
    deps = authoring.dependencies(context, index)
    return {'schema': SCHEMA, 'index': index, 'context': authoring.context_ref(context), 'action': action,
            'label': label or f'Ground decal {action}', 'key': key,
            'request': {'action': action, 'key': key, **({'source': source} if source else {}), 'label': label, **request},
            'before': before, 'after': after, 'model_before_sha256': digest(old), 'model_after_sha256': digest(new),
            'dependencies': deps, 'dependencies_sha256': authoring.canonical(deps)}


def replay(project, state, t, index):
    try:
        ctx = project.context(header=t['context']['header'], cell=t['context']['cell'])
        r = dict(t['request'])
        expected = plan(project, ctx, state, index, **r)
        require(expected == t, 'Decal before-value or dependency differs', 'BEFORE_VALUE_MISMATCH')
        items = state.setdefault('ground_decals', {}).setdefault(ctx['map_member'], {})
        if t['after'] is None:
            items.pop(t['key'], None)
        else:
            items[t['key']] = t['after']
        state['contexts'].append(ctx)
    except (KeyError, TypeError, ValueError) as exc:
        raise EditorError('INVALID_INPUT', 'Malformed decal transaction') from exc


def summary(t):
    return {'operation': 'decal.transaction', 'index': t['index'], 'action': t['action'], 'key': t['key'],
            'context': t['context'], 'label': t['label'], 'before': t['before'], 'after': t['after']}


# ---- geometry --------------------------------------------------------------------------------

def _uv_of(spec, w, h, xs, zs):
    """Texture coordinates (texture units) at global unit positions, rotation/flip applied."""
    cx, cz = spec['x'] * 16, spec['z'] * 16
    dx, dz = xs - cx, zs - cz
    rot = spec['rotation']
    if rot == 90:
        dx, dz = dz, -dx
    elif rot == 180:
        dx, dz = -dx, -dz
    elif rot == 270:
        dx, dz = -dz, dx
    if spec['flip']:
        dx = -dx
    return (dx + w / 2) / w, (dz + h / 2) / h


_DRAPED = {}          # pure-function memo: (model, tileset, decals, sizes, origin) -> draped model bytes
_DRAPED_LIMIT = 64


def drape(project, state, context, raw, tileset):
    """Model bytes with this cell's decals draped over its ground (see module doc)."""
    from . import ground_materials as gm, nitro, surface_format as legacy, surface_native as sn
    import json
    items = decals(state, context['map_member'])
    if not items:
        return raw
    sizes = {n: (w, h) for n, w, h in gm.custom_for(project, state, context)}
    # Replaying a history re-plans every decal edit (old and new model), so identical inputs recur.
    key = (digest(raw), digest(tileset), json.dumps(items, sort_keys=True), tuple(sorted(sizes.items())),
           tuple(context['origin']))
    if key in _DRAPED:
        return _DRAPED[key]
    result = _drape(project, state, context, raw, tileset, items, sizes)
    if len(_DRAPED) >= _DRAPED_LIMIT:
        _DRAPED.pop(next(iter(_DRAPED)))
    _DRAPED[key] = result
    return result


def _drape(project, state, context, raw, tileset, items, sizes):
    from . import ground_materials as gm, surface_format as legacy, surface_native as sn
    summary_, bo, mo, model, shape, draws, by_shape, polys = sn._decode(raw, tileset)
    ox, oz = context['origin']
    ground_names = set(sn.GROUND) | DRAPE_EXTRA
    out = {}
    for key, spec in sorted(items.items()):
        name = gm.texture_name(spec['material'], f"decal:{spec['decal']}")
        w, h = sizes[name]
        x0, z0, x1, z1 = footprint(project, state, spec, context)
        rect = [(x0 - ox) * 16 - 256, (z0 - oz) * 16 - 256, (x1 - ox) * 16 - 256, (z1 - oz) * 16 - 256]
        lift = RISE + LAYER_STEP * spec['layer']
        for sid, p in by_shape.items():
            texture = p.material['texture_name'] or ''
            # Stock ground, floors and custom paving (fills/rims painted earlier); decals are
            # appended only here, so no decal ever drapes over another decal.
            if not (texture in ground_names or 'yuka' in texture or gm.is_custom(texture)):
                continue
            for poly in polys[sid]:
                v = np.asarray(poly)
                normal = np.cross(v[1, :3] - v[0, :3], v[2, :3] - v[0, :3])
                if abs(normal[1]) < STEEPEST * np.linalg.norm(normal):
                    continue                      # walls and cliffs carry no ground decal
                if sn._apart(sn._box(v), rect):
                    continue
                _, inside = legacy.pieces(list(v), rect)
                if len(inside) < 3 or sn.area(inside) < 1e-4:
                    continue
                unit = normal / np.linalg.norm(normal)
                if unit[1] < 0:
                    unit = -unit
                for part in sn.fragments(inside):
                    part = np.array(part, dtype=float)
                    # Lift along the face normal: on a ramp a vertical lift would leave the
                    # decal nearly in the face's plane (hidden or flickering).
                    part[:, :3] += unit * lift
                    gx = part[:, 0] + 256 + ox * 16
                    gz = part[:, 2] + 256 + oz * 16
                    u, t = _uv_of(spec, w, h, gx, gz)
                    part[:, 3], part[:, 4] = u, t
                    out.setdefault(name, []).append(part)
    if not out:
        return raw
    new = {name: (sizes[name], pp) for name, pp in out.items()}
    return sn._rewrite(raw, tileset, summary_, bo, mo, model, shape, draws, by_shape, polys, set(), new)
