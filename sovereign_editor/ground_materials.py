"""Custom ground materials: import, revision, area-scoped variants and export (SURFACE-01).

Contract ``sovereign-ground-material-v1`` (docs/GROUND_MATERIALS.md): a folder with
``material.json`` and indexed PNGs (<= 16 colours, power-of-two sides 8..128)::

    {"schema": "sovereign-ground-material-v1", "id": "cobble", "display": "Cobble paving",
     "fill": "fill.png",                                  # repeating, 16 texels per tile
     "rims": {"grass": "rim_grass.png", "dirt": "rim_dirt.png"},   # road01_r atlas layout
     "decals": {"pile": {"texture": "pile.png"}, ...},    # index 0 transparent
     "variants": {"cove": {"fill": "fill_cove.png"}}}     # same roles and sizes

Roles become tileset textures ``g_<id>_f``, ``g_<id>_r`` (grass rim), ``g_<id>_d`` (dirt rim)
and ``g_<id>_<decal>`` with palettes ``<texture>pl``. Map models bind them by name like every
stock ground material (nitro_writer.extend_map_model); the export gives each stock area
data that shows a material a superset copy of its map tileset (every stock texel/palette
byte kept, the textures of the materials its maps show appended) and rebinds that area
data's map-tileset word, exactly as custom props do for building sets. Because runtime
binding is by name within
the area's tileset, a variant chosen for one area data resolves the same material names
to different texels there, while every other area keeps the base textures.

Packages are content-addressed under ``PROJECT/assets/ground/<id>/<sha16>/`` (source,
baked textures, manifest) and written only when a transaction commits; replay checks every
file hash. Transactions are project-wide (``register``, ``revise``, ``variant``).
Pure planning; Project owns writes and UI/CLI share Project.plan/apply_ground_edit.
"""
import copy
import io
import json
import re
import struct
from pathlib import Path

from .formats import EditorError, digest, require

SCHEMA = 'sovereign-ground-transaction-v1'
CONTRACT = 'sovereign-ground-material-v1'
ACTIONS = ('register', 'revise', 'variant')
ID = re.compile(r'[a-z][a-z0-9]{0,5}')
DECAL_KEY = re.compile(r'[a-z][a-z0-9]{0,4}')
PREFIX = 'g_'
RIM_ROLES = {'grass': 'r', 'dirt': 'd'}
MAX_TEXTURE_BYTES = 12288
FILL_SIZES = (16, 32, 64, 128)


def is_transaction(t):
    return isinstance(t, dict) and t.get('schema') == SCHEMA


def texture_name(material, role):
    """Tileset/model name of one role: 'fill', 'rim:grass', 'rim:dirt' or 'decal:<key>'."""
    if role == 'fill':
        return f'{PREFIX}{material}_f'
    kind, _, key = role.partition(':')
    if kind == 'rim':
        return f'{PREFIX}{material}_{RIM_ROLES[key]}'
    return f'{PREFIX}{material}_{key}'


def palette_name(texture):
    return texture + 'pl'


def is_custom(texture):
    return isinstance(texture, str) and texture.startswith(PREFIX)


# ---- source contract --------------------------------------------------------------------

def _png(raw, what):
    from PIL import Image
    im = Image.open(io.BytesIO(raw))
    require(im.mode == 'P', f'{what}: textures must be indexed PNGs', 'INVALID_ASSET')
    w, h = im.size
    require(w in (8, 16, 32, 64, 128) and h in (8, 16, 32, 64, 128), f'{what}: sides must be 8..128 px powers of two',
            'INVALID_ASSET')
    data = list(im.getdata())
    require(max(data) < 16, f'{what}: use at most 16 palette entries', 'INVALID_ASSET')
    pal = (im.getpalette() or [])[:48]
    pal = pal + [0] * (48 - len(pal))
    words = [(pal[i] >> 3) | (pal[i + 1] >> 3) << 5 | (pal[i + 2] >> 3) << 10 for i in range(0, 48, 3)]
    return {'width': w, 'height': h, 'indices': data, 'palette': words}


def roles(manifest):
    """{role: (source file, cutout)} of the base material."""
    out = {'fill': (manifest['fill'], False)} if manifest.get('fill') else {}
    for key, name in sorted(manifest.get('rims', {}).items()):
        out[f'rim:{key}'] = (name, False)
    for key, d in sorted(manifest.get('decals', {}).items()):
        out[f'decal:{key}'] = (d['texture'], True)
    return out


def load_source(folder):
    """Read an import folder into {'manifest', 'files'} (bytes), validating the contract."""
    folder = Path(folder)
    require(folder.is_dir(), 'Choose a ground-material source folder', 'NOT_FOUND')
    path = folder / 'material.json'
    require(path.is_file(), 'material.json is missing', 'NOT_FOUND')
    try:
        manifest = json.loads(path.read_text())
    except (ValueError, UnicodeDecodeError) as exc:
        raise EditorError('INVALID_ASSET', f'material.json: {exc}') from exc
    require(isinstance(manifest, dict) and manifest.get('schema') == CONTRACT, f'material.json schema must be {CONTRACT}',
            'INVALID_ASSET')
    require(set(manifest) <= {'schema', 'id', 'display', 'fill', 'rims', 'decals', 'variants', 'notes', 'sources'},
            'material.json: unknown fields', 'INVALID_ASSET')
    require(isinstance(manifest.get('id'), str) and ID.fullmatch(manifest['id']),
            'Material id: lowercase letters/digits, 1..6 characters', 'INVALID_ASSET')
    require(isinstance(manifest.get('fill'), str) or manifest.get('fill') is None and manifest.get('decals')
            and not manifest.get('rims'), 'A ground material names its fill texture (decal-only sets name decals and '
            'no rims)', 'INVALID_ASSET')
    rims = manifest.get('rims', {})
    require(isinstance(rims, dict) and set(rims) <= set(RIM_ROLES) and all(isinstance(v, str) for v in rims.values()),
            'rims: {grass, dirt} texture files', 'INVALID_ASSET')
    decals = manifest.get('decals', {})
    require(isinstance(decals, dict) and len(decals) <= 8 and all(
        isinstance(k, str) and DECAL_KEY.fullmatch(k) and isinstance(v, dict) and set(v) == {'texture'}
        and isinstance(v['texture'], str) for k, v in decals.items()),
        'decals: up to 8 {key (1..5 chars): {texture}}', 'INVALID_ASSET')
    variants = manifest.get('variants', {})
    base = roles(manifest)
    require(isinstance(variants, dict) and len(variants) <= 4 and all(
        isinstance(k, str) and DECAL_KEY.fullmatch(k) and k != 'base' and isinstance(v, dict) and v
        and set(v) <= set(base) and all(isinstance(f, str) for f in v.values()) for k, v in variants.items()),
        'variants: up to 4 {name: {role: texture file}} over existing roles', 'INVALID_ASSET')
    sources = manifest.get('sources', [])
    require(isinstance(sources, list) and all(isinstance(s, str) for s in sources) and len(sources) <= 16,
            'sources: editable source files (e.g. .aseprite) kept with the package', 'INVALID_ASSET')
    names = {f for f, _ in base.values()} | {f for v in variants.values() for f in v.values()} | set(sources)
    files = {'material.json': path.read_bytes()}
    for name in sorted(names):
        f = folder / name
        require(f.is_file() and not f.is_symlink() and f.parent == folder, f'Missing source file {name}', 'NOT_FOUND')
        files[name] = f.read_bytes()
    return {'manifest': manifest, 'files': files}


def bake(source):
    """Deterministic textures per variant -> ({variant: [texture dicts]}, report)."""
    from . import nitro_writer as nw
    manifest, files = source['manifest'], source['files']
    mid = manifest['id']
    base = roles(manifest)
    decoded = {}

    def texture(role, filename):
        key = (role, filename)
        if key not in decoded:
            decoded[key] = _png(files[filename], f'{role} ({filename})')
        return decoded[key]

    out, sizes = {}, {}
    for variant in ['base'] + sorted(manifest.get('variants', {})):
        chosen = {**{r: f for r, (f, _) in base.items()}, **(manifest.get('variants', {}).get(variant, {}))}
        textures = []
        for role, (_, cutout) in sorted(base.items()):
            t = texture(role, chosen[role])
            if role == 'fill':
                require(t['width'] in FILL_SIZES and t['height'] in FILL_SIZES, 'The fill repeats: 16..128 px sides',
                        'INVALID_ASSET')
            if role.startswith('rim:'):
                require((t['width'], t['height']) == (32, 32), 'Rims use the stock road01_r atlas layout (32x32)',
                        'INVALID_ASSET')
            size = (t['width'], t['height'])
            require(sizes.setdefault(role, size) == size, f'Variant {variant}: {role} must keep its size {sizes[role]}',
                    'INVALID_ASSET')
            name = texture_name(mid, role)
            textures.append({'name': name, 'palette_name': palette_name(name), 'width': t['width'], 'height': t['height'],
                             'format': nw.FORMAT_PALETTE16, 'color0_transparent': cutout, 'indices': t['indices'],
                             'palette': t['palette'], 'role': role})
        out[variant] = textures
    texel_bytes = sum(t['width'] * t['height'] // 2 + 32 for t in out['base'])
    require(texel_bytes <= MAX_TEXTURE_BYTES, f'Textures use {texel_bytes} bytes; a ground material allows '
            f'{MAX_TEXTURE_BYTES}', 'RESOURCE_CAPACITY')
    report = {'textures': [{'role': t['role'], 'name': t['name'], 'size': [t['width'], t['height']],
                            'cutout': t['color0_transparent']} for t in out['base']],
              'variants': sorted(out), 'texture_bytes': texel_bytes}
    return out, report


def package(source):
    textures, report = bake(source)
    files = {f'source/{k}': v for k, v in sorted(source['files'].items())}
    files['baked/textures.json'] = json.dumps(textures, sort_keys=True, separators=(',', ':')).encode()
    m = source['manifest']
    body = {'contract': CONTRACT, 'id': m['id'], 'display': m.get('display', m['id']),
            'roles': sorted(roles(m)), 'variants': sorted(textures), 'report': report,
            'files': {k: digest(v) for k, v in sorted(files.items())}}
    sha = digest(json.dumps(body, sort_keys=True).encode())
    files['manifest.json'] = json.dumps(body, indent=1, sort_keys=True).encode()
    return sha, files, body


def package_dir(project, mid, sha):
    return project.root / 'assets' / 'ground' / mid / sha[:16]


def pending(project):
    return project.__dict__.setdefault('_ground_pending', {})


def write_package(project, mid, sha, files):
    target = package_dir(project, mid, sha)
    if target.is_dir():
        verify_package(project, mid, sha)
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


def verify_package(project, mid, sha):
    staged = pending(project).get((mid, sha))
    if staged is not None:
        return staged['body']
    cache = project.__dict__.setdefault('_ground_packages', {})
    if (mid, sha) in cache:
        return cache[(mid, sha)]
    folder = package_dir(project, mid, sha)
    require((folder / 'manifest.json').is_file(), f'Ground material package {mid}/{sha[:16]} is missing from the project',
            'MISSING_ASSET')
    body = json.loads((folder / 'manifest.json').read_text())
    require(digest(json.dumps(body, sort_keys=True).encode()) == sha, f'Ground material package {mid} manifest differs',
            'BEFORE_VALUE_MISMATCH')
    for rel, want in body['files'].items():
        path = folder / rel
        require(path.is_file() and digest(path.read_bytes()) == want,
                f'Ground material file {mid}/{rel} differs or is missing', 'BEFORE_VALUE_MISMATCH')
    cache[(mid, sha)] = body
    return body


def baked(project, mid, sha):
    staged = pending(project).get((mid, sha))
    if staged is not None:
        return json.loads(staged['files']['baked/textures.json'])
    verify_package(project, mid, sha)
    return json.loads((package_dir(project, mid, sha) / 'baked/textures.json').read_text())


def stage(project, folder):
    """Bake an import folder into a pending package and return the operation to apply."""
    source = load_source(folder)
    sha, files, body = package(source)
    mid = body['id']
    pending(project)[(mid, sha)] = {'files': files, 'body': body}
    current = materials(project.composed()).get(mid)
    if current is None:
        op = {'action': 'register', 'material': mid, 'package': sha}
    else:
        op = {'action': 'revise', 'material': mid, 'package': sha, 'expected_revision': current['revision']}
    return {'material': mid, 'package': sha, 'report': body['report'], 'display': body['display'],
            'registered': current is not None, 'unchanged': current is not None and current['package'] == sha,
            'users': users(project, project.composed(), mid), 'operation': op}


# ---- composition ------------------------------------------------------------------------

def materials(state):
    return state.get('ground_materials') or {}


def users(project, state, mid):
    """Where a material is shown: painted map cells and decals, with their area data."""
    names = {texture_name(mid, r) for r in body_roles(project, state, mid)}
    out = []
    for member, cells in sorted((state.get('surfaces') or {}).items()):
        count = sum(1 for v in cells.values() if v.get('material') in names)
        if count:
            out.append({'map_member': member, 'tiles': count})
    for member, decals in sorted((state.get('ground_decals') or {}).items()):
        count = sum(1 for d in decals.values() if d['material'] == mid)
        if count:
            out.append({'map_member': member, 'decals': count})
    return out


def body_roles(project, state, mid):
    entry = materials(state).get(mid)
    if entry is None:
        return []
    return verify_package(project, mid, entry['package'])['roles']


def plan(project, state, index, action, **request):
    require(action in ACTIONS, 'Ground material action is register, revise or variant', 'INVALID_INPUT')
    mid = request.get('material')
    require(isinstance(mid, str) and ID.fullmatch(mid), 'Choose a ground material id', 'INVALID_INPUT')
    registry = materials(state)
    t = {'schema': SCHEMA, 'version': 1, 'index': index, 'action': action, 'request': copy.deepcopy(request)}
    if action in ('register', 'revise'):
        require(set(request) == {'material', 'package'} | ({'expected_revision'} if action == 'revise' else set()),
                f'{action} takes material, package' + (' and expected_revision' if action == 'revise' else ''),
                'INVALID_INPUT')
        body = verify_package(project, mid, request['package'])
        require(body['id'] == mid, 'Package id differs from the material id', 'INVALID_INPUT')
        if action == 'register':
            require(mid not in registry, f'Ground material {mid} is already registered; revise it', 'EXISTS')
            t['effect'] = {'revision': 1, 'before': None}
        else:
            current = registry.get(mid)
            require(current is not None, f'Ground material {mid} is not registered', 'NOT_FOUND')
            require(request['expected_revision'] == current['revision'], 'Ground material changed since it was staged',
                    'STALE_ASSET')
            require(current['package'] != request['package'], 'The package is unchanged', 'NO_CHANGE')
            old = set(verify_package(project, mid, current['package'])['roles'])
            used = {r for r in old if _role_used(state, mid, r)}
            require(used <= set(body['roles']), f"A revision keeps every role in use ({', '.join(sorted(used - set(body['roles'])))} missing)",
                    'DEPENDENCY')
            for area, variant in current.get('variants', {}).items():
                require(variant in body['variants'], f'Area {area} uses variant {variant}, which the revision drops',
                        'DEPENDENCY')
            t['effect'] = {'revision': current['revision'] + 1, 'before': current['package']}
        t['impact'] = users(project, state, mid) if mid in registry else []
        return t
    require(set(request) == {'material', 'area_data', 'variant'}, 'variant takes material, area_data and variant',
            'INVALID_INPUT')
    current = registry.get(mid)
    require(current is not None, f'Ground material {mid} is not registered', 'NOT_FOUND')
    area = request['area_data']
    require(type(area) is int and area >= 0, 'Choose a stock area data id', 'INVALID_INPUT')
    from . import world
    from .formats import member_count
    require(area < member_count(project.blob, AREA_ARCHIVE), f'Area data {area} does not exist', 'NOT_FOUND')
    require(world.read_area_data(project.blob, area)['area_type'] == 1, 'Ground materials are qualified for outdoor areas',
            'UNSUPPORTED_CONTEXT')
    body = verify_package(project, mid, current['package'])
    require(request['variant'] in body['variants'], f"Variant is one of {', '.join(body['variants'])}", 'INVALID_INPUT')
    before = current.get('variants', {}).get(str(area), 'base')
    require(before != request['variant'], 'That area already uses this variant', 'NO_CHANGE')
    t['effect'] = {'area_data': area, 'before': before, 'after': request['variant']}
    return t


def _role_used(state, mid, role):
    name = texture_name(mid, role)
    if role.startswith('decal:'):
        key = role.split(':', 1)[1]
        return any(d['material'] == mid and d['decal'] == key
                   for decals in (state.get('ground_decals') or {}).values() for d in decals.values())
    return any(v.get('material') == name for cells in (state.get('surfaces') or {}).values() for v in cells.values())


def apply(state, t):
    registry = state.setdefault('ground_materials', {})
    r = t['request']
    if t['action'] == 'register':
        registry[r['material']] = {'package': r['package'], 'revision': 1, 'variants': {}}
    elif t['action'] == 'revise':
        registry[r['material']] = {**registry[r['material']], 'package': r['package'], 'revision': t['effect']['revision']}
    else:
        entry = registry[r['material']]
        variants = dict(entry.get('variants', {}))
        if r['variant'] == 'base':
            variants.pop(str(r['area_data']), None)
        else:
            variants[str(r['area_data'])] = r['variant']
        registry[r['material']] = {**entry, 'variants': variants}


def replay(project, state, t, index):
    require(is_transaction(t) and t.get('index') == index, 'Ground material transaction out of order', 'STALE_EDIT')
    expected = plan(project, state, index, t['action'], **t['request'])
    require(expected == t, 'Ground material package, revision or variant differs', 'BEFORE_VALUE_MISMATCH')
    apply(state, t)


def describe(t):
    return {'operation': 'ground.transaction', 'index': t['index'], 'action': t['action'], 'request': t['request'],
            'effect': t['effect'], 'impact': t.get('impact', [])}


# ---- textures for painting, the editor and the export ---------------------------------------

AREA_ARCHIVE = 'a/0/4/2'


def area_textures(project, state, area_id, only=None):
    """Textures (base or the area's variant) of every registered material for one area data, or
    of the materials in ``only`` (the export's per-area subset)."""
    out = []
    for mid, entry in sorted(materials(state).items()):
        if only is not None and mid not in only:
            continue
        variant = entry.get('variants', {}).get(str(area_id), 'base')
        out.extend(baked(project, mid, entry['package'])[variant])
    return out


def custom_for(project, state, context):
    """((texture, width, height), ...) the painter may use in one map context."""
    return tuple((t['name'], t['width'], t['height']) for t in area_textures(project, state, context['area_data']['id']))


# ---- stock donor textures of terrain families (waterfall v2, R101-WATERFALL) ------------------
# The ROM's own waterfall art lives in building texture set 18 (Route 47's fall model): the cascade
# ``wfall`` (64x64, 16 colours) and the splash frame ``wfall_c.1`` (16x16, colour 0 clear). A
# v2 waterfall draws them on the map model's area-animation slots river_r (the cascade, transposed
# so the S-only track moves it down the face) and river (foam), so every area data whose maps
# show one gets both in its map tileset, like a ground material. Nothing is invented.
FALL_DONOR_ARCHIVE, FALL_DONOR_SET = 'a/0/7/0', 18
FALL_DONORS = (('river_r', 'wfall', (64, 64), False, True), ('river', 'wfall_c.1', (16, 16), True, False))


def _stock_texture(btx0, name, palette):
    """(indices, palette words, width, height, format, colour-0 clear) straight from TEX0 bytes."""
    from . import nitro_writer as nw
    at = struct.unpack_from('<I', btx0, 16)[0]
    tex0 = btx0[at:]
    h = nw._tex0_header(tex0)
    textures, _ = nw.read_dictionary(tex0, h['tex_dict'])
    palettes, _ = nw.read_dictionary(tex0, h['pal_dict'])
    entry = dict(textures).get(name)
    pal = dict(palettes).get(palette)
    require(entry is not None and pal is not None, f'Stock donor texture {name}/{palette} is absent', 'UNSUPPORTED_RUNTIME')
    params = struct.unpack_from('<I', entry)[0]
    width, height, fmt = 8 << (params >> 20 & 7), 8 << (params >> 23 & 7), params >> 26 & 7
    require(fmt == 3, f'Stock donor texture {name} is not 16-colour', 'UNSUPPORTED_RUNTIME')
    start = h['tex_data'] + (params & 0xFFFF) * 8
    raw = tex0[start:start + width * height // 2]
    indices = [raw[i >> 1] >> (4 * (i & 1)) & 15 for i in range(width * height)]
    pstart = h['pal_data'] + struct.unpack_from('<H', pal)[0] * 8
    words = list(struct.unpack_from('<16H', tex0, pstart))
    return indices, words, width, height, fmt, bool(params >> 29 & 1)


def fall_textures(project):
    """The two waterfall textures a v2 waterfall area adds (tileset texture dicts)."""
    cache = project.__dict__.setdefault('_fall_textures', {})
    key = project.doc['baseline']['sha256']
    if key not in cache:
        from .formats import resource
        btx0 = resource(project.blob, FALL_DONOR_ARCHIVE, FALL_DONOR_SET)[1]
        out = []
        for name, donor, size, clear, transpose in FALL_DONORS:
            indices, words, w, h, fmt, transparent = _stock_texture(btx0, donor, donor + '_pl')
            require((w, h) == size and transparent == clear, f'Stock donor texture {donor} differs from the qualified '
                    'waterfall art', 'UNSUPPORTED_RUNTIME')
            if transpose:
                indices = [indices[x * w + y] for y in range(h) for x in range(w)]
            out.append({'name': name, 'palette_name': name + '_pl', 'width': w, 'height': h, 'format': fmt,
                        'color0_transparent': clear, 'indices': indices, 'palette': words})
        cache[key] = out
    return cache[key]


def fall_areas(project, state):
    """Area data ids whose maps show a v2 waterfall."""
    out = set()
    for items in (state.get('terrain_features') or {}).values():
        for f in items:
            spec = f['spec']
            if spec['action'] == 'waterfall' and spec.get('version', 1) >= 2:
                out.add(project.header(f['header'])['area_data'])
    return out


def with_fall_textures(project, tileset):
    """``tileset`` plus the waterfall textures (for composing a v2 waterfall in any area)."""
    from . import nitro_writer as nw, nitro
    if tileset is None or 'river_r' in nitro.texture_set(tileset)[0]:
        return tileset
    cache = project.__dict__.setdefault('_fall_tilesets', {})
    key = digest(tileset)
    if key not in cache:
        cache[key] = nw.extend_tileset(tileset, fall_textures(project))[0]
    return cache[key]


def editor_tileset(project, state, area_id, stock_bytes):
    """The map tileset the export builds for an area data (stock + material textures)."""
    from . import nitro_writer as nw
    falls = area_id in fall_areas(project, state)
    if (not materials(state) and not falls) or stock_bytes is None:
        return stock_bytes
    textures = area_textures(project, state, area_id) + (fall_textures(project) if falls else [])
    key = (digest(stock_bytes), area_id, falls,
           tuple(sorted((k, e['package'], json.dumps(e.get('variants', {}), sort_keys=True))
                        for k, e in materials(state).items())))
    cache = project.__dict__.setdefault('_ground_tilesets', {})
    if key not in cache:
        cache[key] = nw.extend_tileset(stock_bytes, textures)[0]
    return cache[key]


def shown_by_area(project, state):
    """Stock area data id -> the materials its maps show (painted tiles or decals)."""
    names = {}
    for mid, entry in materials(state).items():
        for role in verify_package(project, mid, entry['package'])['roles']:
            names[texture_name(mid, role)] = mid
    by_member = {}
    for m, cells in (state.get('surfaces') or {}).items():
        for v in cells.values():
            if v.get('material') in names:
                by_member.setdefault(m, set()).add(names[v['material']])
    for m, decals in (state.get('ground_decals') or {}).items():
        for d in decals.values():
            by_member.setdefault(m, set()).add(d['material'])
    areas = {}
    for ctx in state.get('contexts', []):
        if ctx['map_member'] in by_member:
            areas.setdefault(ctx['area_data']['id'], set()).update(by_member[ctx['map_member']])
    return areas


def shown_areas(project, state):
    """Stock area data ids whose maps show a material (painted tiles or decals)."""
    return sorted(shown_by_area(project, state))


def export_plan(project, state):
    """Map-tileset supersets and area-data rebinding for areas that show a material."""
    from . import nitro_writer as nw, props, world
    from .formats import member_count, resource
    falls = fall_areas(project, state)
    if not materials(state) and not falls:
        return None
    shown = shown_by_area(project, state) if materials(state) else {}
    for area_id in falls:
        shown.setdefault(area_id, set())
    areas = sorted(shown)
    if not areas:
        return None
    stock_sets = member_count(project.blob, world.MAP_TEXTURE_ARCHIVE)
    ceiling = props.budget(project)
    appends, changes, report = [], {}, []
    prop_sets = props.area_sets(project, state) if props.assets(state) else {}
    prop_shown = props.set_assets(project, state) if prop_sets else {}
    for area_id in areas:
        area = world.read_area_data(project.blob, area_id)
        stock = resource(project.blob, world.MAP_TEXTURE_ARCHIVE, area['map_tileset'])[1]
        # Only the materials this area's maps show: runtime binding is by name within the area's
        # tileset, so an unused material would only spend the area's texture VRAM.
        textures = area_textures(project, state, area_id, only=shown[area_id])
        if area_id in falls:
            textures = textures + fall_textures(project)
        extended, info = nw.extend_tileset(stock, textures)
        created = stock_sets + len(appends)
        appends.append(extended)
        building = nw.tileset_budget(resource(project.blob, props.TILESET_ARCHIVE, area['buildings_tileset'])[1])
        extra_props = 0
        if area_id in prop_sets:
            registry = props.assets(state)
            extra_props = sum(t['width'] * t['height'] // 2 + 32 for aid in prop_shown[prop_sets[area_id]]
                              for t in props.baked(project, aid, registry[aid]['package'])[1])
        total = info['texel_bytes'] + info['palette_bytes'] + sum(building.values()) + extra_props
        require(total <= ceiling, f'Area {area_id} would load {total} texture bytes; the stock outdoor maximum is {ceiling}',
                'RESOURCE_CAPACITY')
        raw = bytearray(resource(project.blob, AREA_ARCHIVE, area_id)[1])
        require(struct.unpack_from('<H', raw, 2)[0] == area['map_tileset'], 'Area data map tileset differs',
                'BEFORE_VALUE_MISMATCH')
        struct.pack_into('<H', raw, 2, created)
        changes[area_id] = bytes(raw)
        report.append({'area_data': area_id, 'stock_map_tileset': area['map_tileset'], 'created_map_tileset': created,
                       'textures': [t['name'] for t in textures], 'texture_vram_bytes': total,
                       'stock_outdoor_maximum': ceiling})
    return {'appends': {world.MAP_TEXTURE_ARCHIVE: appends}, 'replacements': {AREA_ARCHIVE: changes},
            'report': {'areas': report}}
