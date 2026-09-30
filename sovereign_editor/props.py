"""Custom static props: import contract, baking, project-owned packages and placement.

Contract (``sovereign-static-prop-v1``, docs/CUSTOM_PROPS.md):
* ``asset.json`` + one Wavefront OBJ (triangles/quads, ``usemtl`` per material, UVs
  and normals; units = tiles, +X east, +Y up, +Z south, origin = placement anchor at
  ground) + indexed PNG textures (<= 16 colours, 8..128 px power-of-two sides).
* Materials: own texture (alpha ``opaque`` or ``cutout`` = palette index 0 transparent)
  or a ``stock_texture`` already in the area building tileset (e.g. ``h_kage``) with
  alpha 1..31; ``faces`` front/both; lit (stock prop light 0) or unlit.
* Limits: <= 512 triangles, <= 15 materials, textures <= 16 KiB per prop.

Baking is deterministic: MDL0 without TEX0 (nitro_writer.model) plus the texel/palette
words the export appends to a project-owned copy of the area building tileset. A package
is content-addressed under ``PROJECT/assets/<id>/<sha12>/`` (source + baked + manifest),
written only when a transaction is committed; replay checks every file hash.

Runtime facts (docs/ASSETS_GAMEPLAY_V1 coverage, work ledger): listed models bind
textures by name from the area building set; model IDs must be < 550; the per-model
``bm_field_matshp.dat`` lookup and ``a/1/0/7`` records must cover appended IDs.
Project owns all writes; UI and CLI call Project.plan_prop_edit/apply_prop_edit.
"""
import copy
import hashlib
import io
import json
import math
import re
import struct
from pathlib import Path

from . import authoring, world
from .formats import EditorError, digest, require

SCHEMA = 'sovereign-prop-transaction-v1'
CONTRACT = 'sovereign-static-prop-v1'
MODEL_ARCHIVE, ATTR_ARCHIVE = 'a/0/4/0', 'a/1/0/7'
FIRST_MODEL, MODEL_LIMIT = 340, 550          # stock a/0/4/0 count; Field3dRenderObjManager indexMax
UNITS = 16
MAX_TRIANGLES, MAX_TEXTURE_BYTES = 512, 16384
ACTIONS = ('register', 'revise', 'place', 'duplicate', 'move', 'remove', 'collision')
ID = re.compile(r'[a-z][a-z0-9_]{0,9}')
PROP_LIMIT = 32


def is_transaction(t):
    return isinstance(t, dict) and t.get('schema') == SCHEMA


# ---- source contract --------------------------------------------------------------

def _obj(text):
    v, vt, vn, faces, material = [], [], [], [], None
    for number, line in enumerate(text.splitlines(), 1):
        parts = line.split()
        if not parts or parts[0].startswith('#') or parts[0] in ('mtllib', 'o', 'g', 's'):
            continue
        try:
            if parts[0] == 'v':
                v.append(tuple(float(x) for x in parts[1:4]))
            elif parts[0] == 'vt':
                vt.append(tuple(float(x) for x in parts[1:3]))
            elif parts[0] == 'vn':
                vn.append(tuple(float(x) for x in parts[1:4]))
            elif parts[0] == 'usemtl':
                material = parts[1]
            elif parts[0] == 'f':
                corners = []
                for c in parts[1:]:
                    i = c.split('/')
                    require(len(i) == 3 and i[1] and i[2], f'OBJ line {number}: faces need v/vt/vn', 'INVALID_ASSET')
                    corners.append((int(i[0]) - 1, int(i[1]) - 1, int(i[2]) - 1))
                require(material is not None and 3 <= len(corners) <= 4, f'OBJ line {number}: triangles or quads after usemtl', 'INVALID_ASSET')
                faces.append((material, corners))
            else:
                require(False, f'OBJ line {number}: unsupported statement {parts[0]}', 'INVALID_ASSET')
        except (ValueError, IndexError) as exc:
            raise EditorError('INVALID_ASSET', f'OBJ line {number}: {exc}') from exc
    for _, corners in faces:
        for a, b, c in corners:
            require(0 <= a < len(v) and 0 <= b < len(vt) and 0 <= c < len(vn), 'OBJ index out of range', 'INVALID_ASSET')
    return v, vt, vn, faces


def _png(raw):
    from PIL import Image
    im = Image.open(io.BytesIO(raw))
    require(im.mode == 'P', 'Textures must be indexed PNGs', 'INVALID_ASSET')
    w, h = im.size
    require(w in (8, 16, 32, 64, 128) and h in (8, 16, 32, 64, 128), 'Texture sides must be 8..128 px powers of two', 'INVALID_ASSET')
    data = list(im.getdata())
    colours = max(data) + 1
    require(colours <= 16, 'Textures use at most 16 palette entries (palette16)', 'INVALID_ASSET')
    pal = im.getpalette()[:48] + [0] * max(0, 48 - len(im.getpalette()[:48]))
    words = [(pal[i] >> 3) | (pal[i + 1] >> 3) << 5 | (pal[i + 2] >> 3) << 10 for i in range(0, 48, 3)]
    return {'width': w, 'height': h, 'indices': data, 'palette': words}


def load_source(folder):
    """Read an import folder into {'manifest', 'files'} (bytes), validating the contract."""
    folder = Path(folder)
    require(folder.is_dir(), 'Choose an asset source folder', 'NOT_FOUND')
    manifest = json.loads((folder / 'asset.json').read_text())
    files = {'asset.json': (folder / 'asset.json').read_bytes()}
    require(manifest.get('schema') == CONTRACT, f'asset.json schema must be {CONTRACT}', 'INVALID_ASSET')
    require(isinstance(manifest.get('id'), str) and ID.fullmatch(manifest['id']), 'Asset id: lowercase, <= 10 characters', 'INVALID_ASSET')
    names = [manifest['model']] + sorted({m['texture'] for m in manifest['materials'].values() if 'texture' in m})
    for name in names:
        path = folder / name
        require(path.is_file() and not path.is_symlink() and path.parent == folder, f'Missing source file {name}', 'NOT_FOUND')
        files[name] = path.read_bytes()
    return {'manifest': manifest, 'files': files}


def _material_spec(key, m):
    require(set(m) <= {'texture', 'stock_texture', 'stock_palette', 'size', 'alpha', 'cutout', 'faces', 'lit'},
            f'Material {key}: unknown fields', 'INVALID_ASSET')
    require(('texture' in m) != ('stock_texture' in m), f'Material {key}: choose texture or stock_texture', 'INVALID_ASSET')
    require(m.get('faces', 'front') in ('front', 'both') and isinstance(m.get('lit', True), bool), f'Material {key}: faces/lit', 'INVALID_ASSET')
    alpha = m.get('alpha', 'opaque')
    require(alpha in ('opaque', 'cutout') or type(alpha) is int and 1 <= alpha <= 31, f'Material {key}: alpha', 'INVALID_ASSET')
    require(isinstance(m.get('cutout', False), bool) and (not m.get('cutout') or type(alpha) is int),
            f'Material {key}: cutout combines index-0 transparency with a numeric alpha', 'INVALID_ASSET')
    return alpha


def bake(source):
    """Deterministic conversion -> (model bytes, textures, report). No file writes."""
    from . import nitro_writer as nw
    manifest, files = source['manifest'], source['files']
    aid = manifest['id']
    require(set(manifest) <= {'schema', 'id', 'display', 'model', 'units_per_tile', 'materials', 'collision', 'notes'},
            'asset.json: unknown fields', 'INVALID_ASSET')
    scale = manifest.get('units_per_tile', 1.0)
    require(type(scale) in (int, float) and 0 < scale <= 64, 'units_per_tile must be positive', 'INVALID_ASSET')
    v, vt, vn, faces = _obj(files[manifest['model']].decode('utf-8'))
    mats = manifest['materials']
    require(isinstance(mats, dict) and 1 <= len(mats) < 16, 'asset.json needs 1..15 materials', 'INVALID_ASSET')
    order = sorted({m for m, _ in faces})
    require(set(order) <= set(mats), f'OBJ uses undeclared materials {sorted(set(order) - set(mats))}', 'INVALID_ASSET')
    textures, tex_names, materials = [], {}, []
    for i, key in enumerate(order):
        m = mats[key]; alpha = _material_spec(key, m)
        if 'texture' in m:
            if m['texture'] not in tex_names:
                t = _png(files[m['texture']])
                name = f'{aid}_{len(tex_names)}'
                tex_names[m['texture']] = (name, t)
                textures.append({'name': name, 'palette_name': name + 'pl', 'width': t['width'], 'height': t['height'],
                                 'format': nw.FORMAT_PALETTE16, 'color0_transparent': alpha == 'cutout' or m.get('cutout', False),
                                 'indices': t['indices'], 'palette': t['palette']})
            name, t = tex_names[m['texture']]
            materials.append({'name': f'{aid}_{key}'[:16], 'texture': name, 'palette': name + 'pl', 'width': t['width'],
                              'height': t['height'], 'alpha': 31 if alpha in ('opaque', 'cutout') else alpha,
                              'both_faces': m.get('faces') == 'both', 'lit': m.get('lit', True), 'repeat': False})
        else:
            w, h = m.get('size', [16, 16])
            materials.append({'name': f'{aid}_{key}'[:16], 'texture': m['stock_texture'], 'palette': m.get('stock_palette', m['stock_texture'] + '_pl'),
                              'width': w, 'height': h, 'alpha': 31 if alpha in ('opaque', 'cutout') else alpha,
                              'both_faces': m.get('faces') == 'both', 'lit': m.get('lit', True), 'repeat': False, 'stock': True})
    texel_bytes = sum(t['width'] * t['height'] // 2 + 32 for t in textures)
    require(texel_bytes <= MAX_TEXTURE_BYTES, f'Textures use {texel_bytes} bytes; the prop limit is {MAX_TEXTURE_BYTES}', 'RESOURCE_CAPACITY')
    shapes = []
    import numpy as np
    for mi, key in enumerate(order):
        tris = []
        for mat, corners in faces:
            if mat != key:
                continue
            for a, b, c in ([corners[0], corners[1], corners[2]], [corners[0], corners[2], corners[3]])[:len(corners) - 2]:
                pts = [np.array(v[k[0]]) * UNITS * scale for k in (a, b, c)]
                n = np.array(vn[a[2]])
                if np.dot(np.cross(pts[1] - pts[0], pts[2] - pts[0]), n) < 0:
                    b, c = c, b                                  # stock winding: CCW about the normal
                tris.append({'vertices': [{'position': tuple(float(x) for x in np.array(v[k[0]]) * UNITS * scale),
                                           'uv': (vt[k[1]][0], 1.0 - vt[k[1]][1]), 'normal': vn[k[2]]} for k in (a, b, c)]})
        shapes.append({'name': f'{key}'[:16], 'material': mi, 'triangles': tris})
    ntris = sum(len(s['triangles']) for s in shapes)
    require(0 < ntris <= MAX_TRIANGLES, f'Props need 1..{MAX_TRIANGLES} triangles', 'RESOURCE_CAPACITY')
    model, info = nw.model(aid, materials, shapes)
    collision = manifest.get('collision', [])
    require(isinstance(collision, list) and all(isinstance(c, list) and len(c) == 2 and all(type(k) is int and -8 <= k <= 8 for k in c)
                                                 for c in collision) and len(collision) <= 64,
            'collision: up to 64 [dx, dz] tile offsets from the anchor tile', 'INVALID_ASSET')
    report = {'triangles': ntris, 'materials': len(materials), 'textures': [{'name': t['name'], 'size': [t['width'], t['height']],
              'cutout': t['color0_transparent']} for t in textures], 'texture_bytes': texel_bytes,
              'stock_textures': sorted({m['texture'] for m in materials if m.get('stock')}), 'model_bytes': len(model),
              'bounds_tiles': [[round(c / UNITS, 4) for c in b] for b in info['bounds']], 'position_scale': info['position_scale']}
    return model, textures, report


def package(source):
    """Content-addressed package bytes: {relative path: bytes} and its digest."""
    model, textures, report = bake(source)
    files = {f'source/{k}': v for k, v in sorted(source['files'].items())}
    files['baked/model.nsbmd'] = model
    files['baked/textures.json'] = json.dumps(textures, sort_keys=True, separators=(',', ':')).encode()
    body = {'contract': CONTRACT, 'id': source['manifest']['id'], 'display': source['manifest'].get('display', source['manifest']['id']),
            'collision': source['manifest'].get('collision', []), 'report': report,
            'files': {k: digest(v) for k, v in sorted(files.items())}}
    sha = digest(json.dumps(body, sort_keys=True).encode())
    files['manifest.json'] = json.dumps(body, indent=1, sort_keys=True).encode()
    return sha, files, body


def package_dir(project, aid, sha):
    return project.root / 'assets' / aid / sha[:16]


def write_package(project, aid, sha, files):
    """Idempotent, content-addressed write (called only inside a committing Project lock)."""
    target = package_dir(project, aid, sha)
    if target.is_dir():
        verify_package(project, aid, sha)
        return target
    temp = target.with_name(target.name + '.partial')
    import shutil
    shutil.rmtree(temp, ignore_errors=True)
    for rel, data in files.items():
        path = temp / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    temp.rename(target)
    return target


def verify_package(project, aid, sha):
    """Every file of a committed package exists with its recorded hash; returns manifest.
    A staged (not yet committed) import is served from memory for previews."""
    staged = project.__dict__.get('_prop_pending', {}).get((aid, sha))
    if staged is not None:
        return staged['body']
    cache = project.__dict__.setdefault('_prop_packages', {})
    if (aid, sha) in cache:
        return cache[(aid, sha)]
    folder = package_dir(project, aid, sha)
    require((folder / 'manifest.json').is_file(), f'Asset package {aid}/{sha[:16]} is missing from the project', 'MISSING_ASSET')
    body = json.loads((folder / 'manifest.json').read_text())
    require(digest(json.dumps(body, sort_keys=True).encode()) == sha, f'Asset package {aid} manifest differs', 'BEFORE_VALUE_MISMATCH')
    for rel, want in body['files'].items():
        path = folder / rel
        require(path.is_file() and digest(path.read_bytes()) == want, f'Asset package file {aid}/{rel} differs or is missing', 'BEFORE_VALUE_MISMATCH')
    cache[(aid, sha)] = body
    return body


def baked(project, aid, sha):
    staged = project.__dict__.get('_prop_pending', {}).get((aid, sha))
    if staged is not None:
        return staged['files']['baked/model.nsbmd'], json.loads(staged['files']['baked/textures.json'])
    verify_package(project, aid, sha)
    folder = package_dir(project, aid, sha)
    return (folder / 'baked/model.nsbmd').read_bytes(), json.loads((folder / 'baked/textures.json').read_text())


# ---- composition ------------------------------------------------------------------

# Readers never add keys: a composed state without props must stay byte-identical
# (composition snapshots digest it). Writers use _store.
def assets(state):
    return state.get('prop_assets') or {}


def instances(state):
    return state.get('prop_instances') or {}


def owners(state):
    return state.get('prop_collision') or {}


def _store(state, key):
    return state.setdefault(key, {})


def _context(project, ref):
    ctx = project.context(header=ref['header'], cell=ref['cell'])
    require(authoring.context_ref(ctx) == ref, 'Prop context no longer resolves to the authored cell', 'CONTEXT_MISMATCH')
    return ctx


def _cells(ctx, x, z, collision):
    tx, tz = math.floor(x), math.floor(z)
    out = []
    for dx, dz in collision:
        cx, cz = tx + dx, tz + dz
        require(ctx['origin'][0] <= cx < ctx['origin'][0] + world.MAP_SIZE and ctx['origin'][1] <= cz < ctx['origin'][1] + world.MAP_SIZE,
                f'Collision tile {cx},{cz} leaves map cell {ctx["cell"]["x"]},{ctx["cell"]["y"]}', 'OUTSIDE_MAP')
        out.append((cx, cz))
    return sorted(set(out))


def _record(project, ctx, state, model_id, x, z):
    from . import scenery
    table = scenery.table_for(project, ctx, state)
    donor = next(iter(table.values()), None)
    require(donor is not None, 'This map has no stock placement to copy the record layout from', 'UNSUPPORTED_CONTEXT')
    height = scenery.floor_height(project, ctx, {'x': x, 'z': z})
    words = authoring.record_from_global(ctx, {'x': x, 'z': z}, {'y': world.record_value(height)})
    raw = bytearray(donor['raw'])
    struct.pack_into('<I3i', raw, 0, model_id, words['x'], words['y'], words['z'])
    raw[16:28] = bytes(12)
    struct.pack_into('<3i', raw, 28, 4096, 4096, 4096)
    return bytes(raw), height


def _claim(project, ctx, state, key, cells):
    """Give instance ``key`` the blocked bit of ``cells``; returns permission changes."""
    member = ctx['map_member']; raw = project.member_raw(member); changes = []
    own = _store(state, 'prop_collision')
    for x, z in cells:
        offset = world.cell_offset(ctx, x, z)
        current = state['permissions'].get((member, offset), raw[offset:offset + 2])
        entry = own.get((member, offset))
        if entry is None:
            entry = own[(member, offset)] = {'base': current.hex(), 'owners': [], 'x': x, 'z': z}
        else:
            require(current.hex() == _pair(entry).hex(), f'Collision tile {x},{z} was changed outside the prop layer', 'COLLISION_CONFLICT')
        require(key not in entry['owners'], 'Duplicate collision claim', 'STALE_EDIT')
        entry['owners'] = sorted(entry['owners'] + [key])
        after = _pair(entry)
        if after != current:
            changes.append({'x': x, 'z': z, 'offset': offset, 'before': current.hex(), 'after': after.hex()})
        state['permissions'][(member, offset)] = after
        state['generic']['permissions'][(member, offset)] = after
    return changes


def _release(project, ctx, state, key, cells):
    member = ctx['map_member']; changes = []
    own = _store(state, 'prop_collision')
    for x, z in cells:
        offset = world.cell_offset(ctx, x, z)
        entry = own.get((member, offset))
        require(entry is not None and key in entry['owners'], f'Instance {key} does not own collision tile {x},{z}', 'STALE_EDIT')
        current = state['permissions'].get((member, offset), project.member_raw(member)[offset:offset + 2])
        require(current.hex() == _pair(entry).hex(), f'Collision tile {x},{z} was changed outside the prop layer', 'COLLISION_CONFLICT')
        entry['owners'] = [o for o in entry['owners'] if o != key]
        after = _pair(entry)
        if not entry['owners']:
            del own[(member, offset)]
        if after != current:
            changes.append({'x': x, 'z': z, 'offset': offset, 'before': current.hex(), 'after': after.hex()})
        state['permissions'][(member, offset)] = after
        state['generic']['permissions'][(member, offset)] = after
    return changes


def _pair(entry):
    base = bytes.fromhex(entry['base'])
    return bytes((base[0], base[1] | 0x80)) if entry['owners'] else base


def _scratch(state, member):
    """Shallow overlay of the containers a prop transaction mutates (plan never writes)."""
    work = dict(state)
    work['permissions'] = dict(state['permissions'])
    work['generic'] = {**state['generic'], 'permissions': dict(state['generic']['permissions'])}
    work['objects'] = dict(state['objects'])
    if member is not None and member in state['objects']:
        work['objects'][member] = dict(state['objects'][member])
    work['placements'] = dict(state['placements'])
    work['structural_members'] = set(state['structural_members'])
    work['contexts'] = list(state['contexts'])
    for key in ('prop_assets', 'prop_instances', 'prop_collision'):
        work[key] = copy.deepcopy(state.get(key, {}))
    return work


def plan(project, context, state, index, action, _apply=False, **request):
    """One prop transaction. Plans on a scratch overlay; replay applies with _apply=True."""
    require(action in ACTIONS, 'Unknown prop action', 'INVALID_INPUT')
    if not _apply:
        state = _scratch(state, context['map_member'] if context else None)
    t = {'schema': SCHEMA, 'version': 1, 'index': index, 'action': action, 'request': copy.deepcopy(request)}
    registry, placed = _store(state, 'prop_assets'), _store(state, 'prop_instances')
    if action in ('register', 'revise'):
        require(set(request) == {'asset', 'package'} | ({'expected_revision'} if action == 'revise' else set()),
                'Register/revise take asset, package (and expected_revision)', 'INVALID_INPUT')
        aid, sha = request['asset'], request['package']
        body = verify_package(project, aid, sha)
        require(body['id'] == aid, 'Package id differs from the asset id', 'INVALID_ASSET')
        if action == 'register':
            require(aid not in registry, f'Asset {aid} is already registered; revise it instead', 'EXISTS')
            model_id = FIRST_MODEL + len([a for a in registry.values()])
            require(model_id < MODEL_LIMIT, 'The field renderer indexes building models below 550', 'RESOURCE_CAPACITY')
            names = {n for a in registry.values() for n in a['texture_names']}
            model, textures = baked(project, aid, sha)
            require(not names & {x['name'] for x in textures}, 'Texture name collides with another asset', 'NAME_CONFLICT')
            registry[aid] = {'revision': 1, 'package': sha, 'model_id': model_id, 'display': body['display'],
                             'texture_names': sorted(x['name'] for x in textures), 'report': body['report'],
                             'collision': body['collision']}
            t['effect'] = {'model_id': model_id, 'revision': 1}
        else:
            require(aid in registry, f'Asset {aid} is not registered', 'NOT_FOUND')
            old = registry[aid]
            require(request['expected_revision'] == old['revision'], f"Asset {aid} is at revision {old['revision']}, not "
                    f"{request['expected_revision']}; reload before revising", 'STALE_ASSET')
            require(sha != old['package'], 'Unchanged reimport: the package is identical', 'NO_CHANGE')
            model, textures = baked(project, aid, sha)
            require(sorted(x['name'] for x in textures) == old['texture_names'],
                    'A revision keeps the texture set (names); register a new asset for a different set', 'UNSUPPORTED_ASSET')
            affected = sorted(k for k, i in placed.items() if i['asset'] == aid)
            registry[aid] = {**old, 'revision': old['revision'] + 1, 'package': sha, 'display': body['display'],
                             'report': body['report'], 'collision': body['collision']}
            for k in affected:
                placed[k]['revision'] = registry[aid]['revision']
            t['effect'] = {'from': {'revision': old['revision'], 'package': old['package']},
                           'to': {'revision': old['revision'] + 1, 'package': sha}, 'instances': affected,
                           'note': 'Instances follow the asset: its model member and textures change; placements and collision do not.'}
        t['context'] = None
        return t
    ctx = context
    ref = authoring.context_ref(ctx)
    member = ctx['map_member']
    from . import scenery
    table = scenery.table_for(project, ctx, state)
    if action in ('place', 'duplicate'):
        if action == 'place':
            require(set(request) == {'asset', 'instance', 'x', 'z', 'collision', 'expected_revision'}, 'Place fields: asset, instance, x, z, collision, expected_revision', 'INVALID_INPUT')
            aid = request['asset']
            require(aid in registry, f'Asset {aid} is not registered', 'NOT_FOUND')
            require(request['expected_revision'] == registry[aid]['revision'],
                    f"Asset {aid} is at revision {registry[aid]['revision']}; refresh the palette", 'STALE_ASSET')
            collision = request['collision']
        else:
            require(set(request) == {'source', 'instance', 'x', 'z'}, 'Duplicate fields: source, instance, x, z', 'INVALID_INPUT')
            src = placed.get(request['source'])
            require(src is not None, f"Instance {request['source']} does not exist", 'NOT_FOUND')
            require(src['context'] == ref, 'Duplicate within the source instance map cell', 'CONTEXT_MISMATCH')
            aid, collision = src['asset'], src['collision']
        key = request['instance']
        require(isinstance(key, str) and ID.fullmatch(key.replace('-', '_')[:10]) and len(key) <= 24, 'Instance names: lowercase, <= 24', 'INVALID_INPUT')
        require(key not in placed, f'Instance {key} already exists', 'EXISTS')
        require(isinstance(collision, list) and all(isinstance(c, list) and len(c) == 2 and all(type(k) is int and -8 <= k <= 8 for k in c) for c in collision),
                'Collision is a list of [dx, dz] tile offsets', 'INVALID_INPUT')
        position = {'x': request['x'], 'z': request['z']}
        authoring.require_anchor(ctx, position)
        require(len(table) < PROP_LIMIT, f'This map already has {PROP_LIMIT} placed objects (runtime limit)', 'RESOURCE_CAPACITY')
        raw, height = _record(project, ctx, state, registry[aid]['model_id'], request['x'], request['z'])
        slot = len(project.member_data(member)[1]) + index
        require(slot not in table, 'Created object slot already exists', 'STALE_EDIT')
        cells = _cells(ctx, request['x'], request['z'], collision)
        for x, z in cells:
            offset = world.cell_offset(ctx, x, z)
            pair = state['permissions'].get((member, offset), project.member_raw(member)[offset:offset + 2])
            require(not world.is_blocked(pair) or (member, offset) in owners(state),
                    f'Collision tile {x},{z} is already blocked by terrain or another object; choose free tiles', 'BLOCKED_TILE')
        table[slot] = {'id': f'prop:{key}', 'raw': raw}
        state['placements'][(member, slot)] = dict(zip(('x', 'y', 'z'), struct.unpack_from('<3i', raw, 4)))
        state['structural_members'].add(member)
        changes = _claim(project, ctx, state, key, cells)
        placed[key] = {'asset': aid, 'revision': registry[aid]['revision'], 'context': ref, 'member': member, 'slot': slot,
                       'x': request['x'], 'z': request['z'], 'height': height, 'collision': [list(c) for c in collision],
                       'cells': [list(c) for c in cells]}
        t['effect'] = {'instance': key, 'slot': slot, 'record': raw.hex(), 'cells': [list(c) for c in cells], 'permissions': changes}
    else:
        key = request.get('instance')
        inst = placed.get(key)
        require(inst is not None, f'Instance {key} does not exist', 'NOT_FOUND')
        require(inst['context'] == ref, 'Choose the instance map cell', 'CONTEXT_MISMATCH')
        cells = [tuple(c) for c in inst['cells']]
        if action == 'remove':
            require(set(request) == {'instance'}, 'Remove takes instance', 'INVALID_INPUT')
            changes = _release(project, ctx, state, key, cells)
            del table[inst['slot']]
            state['structural_members'].add(member)
            del placed[key]
            t['effect'] = {'instance': key, 'slot': inst['slot'], 'permissions': changes}
        else:
            if action == 'move':
                require(set(request) == {'instance', 'x', 'z'}, 'Move fields: instance, x, z', 'INVALID_INPUT')
                x, z, collision = request['x'], request['z'], inst['collision']
                require((x, z) != (inst['x'], inst['z']), 'The instance is already there', 'NO_CHANGE')
                authoring.require_anchor(ctx, {'x': x, 'z': z})
            else:
                require(set(request) == {'instance', 'collision'}, 'Collision fields: instance, collision', 'INVALID_INPUT')
                x, z, collision = inst['x'], inst['z'], request['collision']
                require(isinstance(collision, list) and all(isinstance(c, list) and len(c) == 2 and all(type(k) is int and -8 <= k <= 8 for k in c) for c in collision),
                        'Collision is a list of [dx, dz] tile offsets', 'INVALID_INPUT')
                require(sorted(map(tuple, collision)) != sorted(map(tuple, inst['collision'])), 'Collision is unchanged', 'NO_CHANGE')
            released = _release(project, ctx, state, key, cells)
            new_cells = _cells(ctx, x, z, collision)
            for cx, cz in new_cells:
                offset = world.cell_offset(ctx, cx, cz)
                pair = state['permissions'].get((member, offset), project.member_raw(member)[offset:offset + 2])
                require(not world.is_blocked(pair) or (member, offset) in owners(state),
                        f'Collision tile {cx},{cz} is already blocked by terrain or another object', 'BLOCKED_TILE')
            claimed = _claim(project, ctx, state, key, new_cells)
            raw, height = _record(project, ctx, state, assets(state)[inst['asset']]['model_id'], x, z)
            table[inst['slot']] = {'id': f'prop:{key}', 'raw': raw}
            state['placements'][(member, inst['slot'])] = dict(zip(('x', 'y', 'z'), struct.unpack_from('<3i', raw, 4)))
            state['structural_members'].add(member)
            inst.update(x=x, z=z, height=height, collision=[list(c) for c in collision], cells=[list(c) for c in new_cells])
            merged = {}
            for c in released + claimed:
                k = c['offset']
                merged[k] = {**c, 'before': merged[k]['before']} if k in merged else dict(c)
            t['effect'] = {'instance': key, 'record': raw.hex(), 'cells': [list(c) for c in new_cells],
                           'permissions': [c for c in merged.values() if c['before'] != c['after']]}
    t['context'] = ref
    state['contexts'].append(ctx)
    return t


def replay(project, state, transaction, index):
    require(is_transaction(transaction) and transaction.get('index') == index, 'Prop transaction out of order', 'STALE_EDIT')
    ref = transaction.get('context')
    ctx = _context(project, ref) if ref else None
    expected = plan(project, ctx, state, index, transaction['action'], _apply=True, **transaction['request'])
    require(expected == transaction, 'Prop before-values, package or ownership differ', 'BEFORE_VALUE_MISMATCH')


def summary(t):
    return {'operation': 'prop.transaction', 'index': t['index'], 'action': t['action'], 'context': t['context'],
            'request': t['request'], 'effect': t.get('effect')}


def owned_cells(state):
    return {k: v['owners'] for k, v in owners(state).items()}


def view(project, state=None):
    """Registry, instances and ownership for UI/CLI (read-only)."""
    state = project.composed() if state is None else state
    return {'assets': copy.deepcopy(assets(state)), 'instances': copy.deepcopy(instances(state)),
            'collision_owners': [{'member': m, 'offset': o, 'x': e['x'], 'z': e['z'], 'owners': e['owners'], 'base': e['base']}
                                 for (m, o), e in sorted(owners(state).items())],
            'limits': {'model_ids': [FIRST_MODEL, MODEL_LIMIT - 1], 'objects_per_map': PROP_LIMIT,
                       'triangles': MAX_TRIANGLES, 'texture_bytes': MAX_TEXTURE_BYTES}}


# ---- export -------------------------------------------------------------------------

TILESET_ARCHIVE, BUILD_LIST_ARCHIVE, AREA_ARCHIVE = 'a/0/7/0', 'a/0/4/3', 'a/0/4/2'
MATSHP = 'fielddata/build_model/bm_field_matshp.dat'
STATIC_ATTRIBUTE_DONOR = 52          # yo_sp2 planter: default static record (no animation)


def pending(project):
    return project.__dict__.setdefault('_prop_pending', {})


def area_assets(project, state):
    """Stock area data id -> the registered assets its maps show: placed instances and any other
    map object using a custom model (e.g. one copied with a created area's cells)."""
    registry = assets(state)
    by_model = {a['model_id']: aid for aid, a in registry.items()}
    used = {}

    def add(ctx, aid):
        area = ctx['area_data']
        require(area['area_type'] == 1, 'Custom props are qualified for outdoor areas only', 'UNSUPPORTED_CONTEXT')
        used.setdefault(area['id'], set()).add(aid)
    for inst in instances(state).values():
        add(project.context(header=inst['context']['header'], cell=inst['context']['cell']), inst['asset'])
    if by_model:
        from . import scenery, world_authoring
        members = {}
        for ctx in state.get('contexts', []):
            members.setdefault(ctx['map_member'], ctx)
        for area in world_authoring.areas(state).values():
            for cell in area['cells']:
                ctx = project.context(header=area['header'], cell=cell['cell'])
                members.setdefault(ctx['map_member'], ctx)
        for member, ctx in sorted(members.items()):
            for obj in scenery.table_for(project, ctx, state).values():
                model = struct.unpack_from('<I', obj['raw'])[0]
                if model in by_model:
                    add(ctx, by_model[model])
    return used


def area_sets(project, state):
    """Stock area data used by maps that show custom props -> their building set id."""
    return {aid: world.read_area_data(project.blob, aid)['buildings_tileset'] for aid in area_assets(project, state)}


def set_assets(project, state):
    """Building set id -> the assets shown by the areas that load it (sorted by model id)."""
    registry = assets(state)
    out = {}
    for area_id, used in area_assets(project, state).items():
        out.setdefault(world.read_area_data(project.blob, area_id)['buildings_tileset'], set()).update(used)
    return {k: sorted(v, key=lambda aid: registry[aid]['model_id']) for k, v in out.items()}


def budget(project):
    """Stock texture-VRAM envelope: the largest map + building tileset pair any outdoor area loads."""
    cached = getattr(project, '_prop_budget', None)
    if cached is None:
        from . import nitro_writer as nw
        from .formats import member_count, resource
        best = 0
        for aid in range(member_count(project.blob, AREA_ARCHIVE)):
            try:
                area = world.read_area_data(project.blob, aid)
                if area['area_type'] != 1:
                    continue
                total = 0
                for arc, member in ((world.MAP_TEXTURE_ARCHIVE, area['map_tileset']), (TILESET_ARCHIVE, area['buildings_tileset'])):
                    b = nw.tileset_budget(resource(project.blob, arc, member)[1])
                    total += b['texel_bytes'] + b['compressed_bytes'] + b['palette_bytes']
                best = max(best, total)
            except (EditorError, struct.error, ValueError):
                continue
        cached = project._prop_budget = best
    return cached


def export_plan(project, state):
    """Resources the export appends/replaces for registered assets and placed instances."""
    from . import nitro_writer as nw
    from .formats import member_count, resource, file_span
    registry = assets(state)
    if not registry:
        return None
    ordered = sorted(registry.items(), key=lambda kv: kv[1]['model_id'])
    require([a['model_id'] for _, a in ordered] == list(range(FIRST_MODEL, FIRST_MODEL + len(ordered))),
            'Asset model IDs must follow the stock building-model count', 'RESOURCE_CONFLICT')
    require(member_count(project.blob, MODEL_ARCHIVE) == FIRST_MODEL and member_count(project.blob, ATTR_ARCHIVE) == FIRST_MODEL,
            'Building model archives differ from the qualified 340-member layout', 'UNSUPPORTED_RUNTIME')
    models, textures = [], {}
    for aid, a in ordered:
        model, tex = baked(project, aid, a['package'])
        models.append(model); textures[aid] = tex
    attr = resource(project.blob, ATTR_ARCHIVE, STATIC_ATTRIBUTE_DONOR)[1]
    require(attr.hex() == 'ffff000000000000' + 'ff' * 16, 'Static attribute donor differs', 'UNSUPPORTED_RUNTIME')
    sets = area_sets(project, state)
    # Each created set carries only the assets its areas show; the model archive keeps all.
    shown = set_assets(project, state)
    stock_sets = member_count(project.blob, TILESET_ARCHIVE)
    require(member_count(project.blob, BUILD_LIST_ARCHIVE) == stock_sets, 'Build lists and building tilesets differ in count', 'UNSUPPORTED_RUNTIME')
    new_sets, areas, report = {}, {}, []
    for area_id, set_id in sorted(sets.items()):
        if set_id not in new_sets:
            new_sets[set_id] = stock_sets + len(new_sets)
        areas[area_id] = new_sets[set_id]
    tilesets, lists = [], []
    ceiling = budget(project)
    for set_id, created in sorted(new_sets.items(), key=lambda kv: kv[1]):
        stock_tex = resource(project.blob, TILESET_ARCHIVE, set_id)[1]
        extended, info = nw.extend_tileset(stock_tex, [t for aid in shown[set_id] for t in textures[aid]])
        stock_list = resource(project.blob, BUILD_LIST_ARCHIVE, set_id)[1]
        count = struct.unpack_from('<H', stock_list)[0]
        ids = list(struct.unpack_from(f'<{count}H', stock_list, 2)) + [registry[aid]['model_id'] for aid in shown[set_id]]
        require(len(ids) < 0x226, 'Build list exceeds the runtime limit of 549 models', 'RESOURCE_CAPACITY')
        new_list = struct.pack(f'<H{len(ids)}H', len(ids), *ids)
        new_list += stock_list[2 + 2 * count:] if len(stock_list) > 2 + 2 * count else b''
        tilesets.append(extended); lists.append(new_list)
        users = [a for a, s in areas.items() if s == created]
        for area_id in users:
            area = world.read_area_data(project.blob, area_id)
            map_b = nw.tileset_budget(resource(project.blob, world.MAP_TEXTURE_ARCHIVE, area['map_tileset'])[1])
            total = info['texel_bytes'] + info['palette_bytes'] + sum(map_b.values())
            require(total <= ceiling, f'Area {area_id} would load {total} texture bytes; the stock outdoor maximum is {ceiling}',
                    'RESOURCE_CAPACITY')
            report.append({'area_data': area_id, 'stock_set': set_id, 'created_set': created, 'texture_vram_bytes': total,
                           'stock_outdoor_maximum': ceiling, 'models': [registry[aid]['model_id'] for aid in shown[set_id]]})
    area_changes = {}
    for area_id, created in areas.items():
        raw = bytearray(resource(project.blob, AREA_ARCHIVE, area_id)[1])
        require(struct.unpack_from('<H', raw)[0] == sets[area_id], 'Area data building set differs', 'BEFORE_VALUE_MISMATCH')
        struct.pack_into('<H', raw, 0, created)
        area_changes[area_id] = bytes(raw)
    _, matshp = file_span(project.blob, MATSHP)
    count, pairs = struct.unpack_from('<2H', matshp)
    require(count == FIRST_MODEL and len(matshp) == 4 + 4 * count + 4 * pairs, 'bm_field_matshp.dat layout differs', 'UNSUPPORTED_RUNTIME')
    extra = len(ordered)
    new_matshp = struct.pack('<2H', count + extra, pairs) + matshp[4:4 + 4 * count] + bytes(4 * extra) + matshp[4 + 4 * count:]
    return {'appends': {MODEL_ARCHIVE: models, ATTR_ARCHIVE: [attr] * len(models), TILESET_ARCHIVE: tilesets,
                        BUILD_LIST_ARCHIVE: lists},
            'replacements': {AREA_ARCHIVE: area_changes}, 'files': {MATSHP: new_matshp},
            'report': {'model_ids': [a['model_id'] for _, a in ordered], 'created_sets': new_sets, 'area_rebinding': areas,
                       'budgets': report}}


# ---- editor rendering -----------------------------------------------------------------

def editor_model(project, model_id):
    """Baked bytes of a registered asset's model member, or None for stock IDs."""
    if model_id < FIRST_MODEL:
        return None
    for aid, a in assets(project.composed()).items():
        if a['model_id'] == model_id:
            return baked(project, aid, a['package'])[0]
    require(False, f'Building model {model_id} is not a registered custom prop', 'NOT_FOUND')


def editor_tileset(project, stock_bytes):
    """The building tileset the export builds for areas with props (stock + asset textures)."""
    from . import nitro_writer as nw
    registry = assets(project.composed())
    if not registry or stock_bytes is None:
        return stock_bytes
    key = (digest(stock_bytes), tuple(sorted((k, a['package']) for k, a in registry.items())))
    cache = project.__dict__.setdefault('_prop_tilesets', {})
    if key not in cache:
        textures = []
        for aid, a in sorted(registry.items(), key=lambda kv: kv[1]['model_id']):
            textures.extend(baked(project, aid, a['package'])[1])
        try:
            cache[key] = nw.extend_tileset(stock_bytes, textures)[0]
        except EditorError:
            cache[key] = stock_bytes        # e.g. a compressed-texture set: shown without props
    return cache[key]
