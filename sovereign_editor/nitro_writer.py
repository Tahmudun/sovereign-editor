"""Bounded Nitro writers for custom static props: dictionaries, TEX0 and MDL0.

Qualified against the pinned ROM (tests/test_assets.py, tools/asset_qualification.py):
* ``dictionary`` rebuilds every stock building-tileset and building-model dictionary
  byte for byte (Patricia tree nodes included), so the runtime name lookup
  (NNS_G3dGetResDictIdxByName, Patricia search above 15 entries) sees stock layout.
* ``extend_tileset`` appends textures/palettes to a stock BTX0: existing texel and
  palette bytes, formats and addresses are unchanged; only dictionaries and block
  offsets grow. ``decode_model``/``texture_set`` of nitro.py read the result back.
* ``model`` writes one static MDL0 (identity node, triangles/quads with UV, normals
  and per-material polygon attributes copied from stock props); no TEX0 — the field
  binds textures by name from the area building tileset (AreaDataManager_Load).
Anything outside this subset (animation, skinning, matrices, texture transforms,
vertex colours per vertex, >1 node) is refused.
"""
import math
import struct

from .formats import require

MAX_NAME = 16


def _name(name):
    raw = name.encode('ascii')
    require(0 < len(raw) <= MAX_NAME and all(32 < c < 127 for c in raw), f'Invalid Nitro name {name!r}', 'INVALID_ASSET')
    return raw.ljust(16, b'\0')


def _bit(name, bit):
    words = struct.unpack('<4I', name)
    return (words[bit >> 5] >> (bit & 31)) & 1


def patricia(names):
    """NNS dictionary tree nodes [refBit, left, right, entry] for 16-byte names.

    Sedgewick Patricia insertion in entry order with the NNS conventions: node 0 is
    the header (refBit 127, left -> first node), bits are tested from the most
    significant differing bit of the 128-bit little-endian name words.
    """
    nodes = [[127, 0, 0, 0]]
    for index, name in enumerate(names):
        # Closest existing leaf.
        p, x = 0, nodes[0][1]
        while nodes[p][0] > nodes[x][0]:
            p, x = x, (nodes[x][2] if _bit(name, nodes[x][0]) else nodes[x][1])
        other = names[nodes[x][3]] if x else bytes(16)
        diff = [b for b in range(127, -1, -1) if _bit(name, b) != _bit(other, b)]
        require(diff, 'Duplicate dictionary name', 'INVALID_ASSET')
        bit = diff[0]
        p, x = 0, nodes[0][1]
        while nodes[p][0] > nodes[x][0] and nodes[x][0] > bit:
            p, x = x, (nodes[x][2] if _bit(name, nodes[x][0]) else nodes[x][1])
        new = len(nodes)
        nodes.append([bit, x, new, index] if _bit(name, bit) else [bit, new, x, index])
        if p == 0:
            nodes[0][1] = new
        elif _bit(name, nodes[p][0]):
            nodes[p][2] = new
        else:
            nodes[p][1] = new
    # The converter stores nodes in preorder (node, then left, then right subtree);
    # links pointing up the tree are back-links to entries and are not descended.
    order = [0]

    def visit(i):
        order.append(i)
        for child in (nodes[i][1], nodes[i][2]):
            if nodes[child][0] < nodes[i][0] and child not in order:
                visit(child)
    if names:
        visit(nodes[0][1])
    remap = {old: new for new, old in enumerate(order)}
    return [[nodes[o][0], remap[nodes[o][1]], remap[nodes[o][2]], nodes[o][3]] for o in order]


def lookup(raw, name):
    """NNS_G3dGetResDictIdxByName on dictionary bytes (Patricia path for any size)."""
    count = raw[1]
    key = _name(name)
    nodes = [struct.unpack_from('4B', raw, 8 + 4 * i) for i in range(count + 1)]
    p, x = nodes[0], nodes[nodes[0][1]]
    while p[0] > x[0]:
        p, x = x, nodes[x[2] if _bit(key, x[0]) else x[1]]
    entry_offset = struct.unpack_from('<H', raw, 6)[0]
    unit, names = struct.unpack_from('<HH', raw, entry_offset)
    stored = raw[entry_offset + names + 16 * x[3]:entry_offset + names + 16 * x[3] + 16]
    return x[3] if stored == key else None


def dictionary(names, entries):
    """Complete NNS dictionary block: header, Patricia nodes, entry data and names."""
    keys = [_name(n) for n in names]
    require(len(set(keys)) == len(keys) and len(keys) < 256, 'Dictionary names must be unique (< 256)', 'INVALID_ASSET')
    unit = len(entries[0]) if entries else 4
    require(len(entries) == len(keys) and all(len(e) == unit for e in entries) and unit in (4, 8),
            'Dictionary entries must share a 4- or 8-byte unit', 'INVALID_ASSET')
    n = len(keys)
    nodes = patricia(keys)
    offset = 8 + 4 * (n + 1)
    size = offset + 4 + unit * n + 16 * n
    return (struct.pack('<BBHHH', 0, n, size, 8, offset) + b''.join(struct.pack('4B', *node) for node in nodes)
            + struct.pack('<HH', unit, 4 + unit * n) + b''.join(entries) + b''.join(keys))


def read_dictionary(raw, offset=0):
    """[(name, entry bytes)] of a dictionary at ``offset``."""
    count, size = raw[offset + 1], struct.unpack_from('<H', raw, offset + 2)[0]
    entry = offset + struct.unpack_from('<H', raw, offset + 6)[0]
    unit, names = struct.unpack_from('<HH', raw, entry)
    return [(raw[entry + names + 16 * i:entry + names + 16 * i + 16].rstrip(b'\0').decode('ascii'),
             raw[entry + 4 + unit * i:entry + 4 + unit * (i + 1)]) for i in range(count)], size


# ---- textures -----------------------------------------------------------------------

FORMAT_PALETTE4, FORMAT_PALETTE16, FORMAT_PALETTE256 = 2, 3, 4
BITS = {FORMAT_PALETTE4: 2, FORMAT_PALETTE16: 4, FORMAT_PALETTE256: 8}
SIZES = (8, 16, 32, 64, 128, 256, 512, 1024)


def pack_texels(indices, width, height, fmt):
    bits = BITS[fmt]
    require(len(indices) == width * height and max(indices, default=0) < (1 << bits), 'Texel index out of range', 'INVALID_ASSET')
    per = 8 // bits
    return bytes(sum(indices[i + k] << (bits * k) for k in range(per)) for i in range(0, len(indices), per))


def rgb555(color):
    r, g, b = color[:3]
    return (r >> 3) | (g >> 3) << 5 | (b >> 3) << 10


def _tex0_header(raw):
    require(raw[:4] == b'TEX0', 'Not a TEX0 block', 'INVALID_ASSET')
    (tex_size, tex_dict, tex_data) = struct.unpack_from('<HHxxxxI', raw, 12)
    return {'tex_size8': tex_size, 'tex_dict': tex_dict, 'tex_data': tex_data,
            'comp_size8': struct.unpack_from('<H', raw, 28)[0], 'comp_info': struct.unpack_from('<H', raw, 30)[0],
            'comp_data': struct.unpack_from('<I', raw, 36)[0], 'comp_index': struct.unpack_from('<I', raw, 40)[0],
            'pal_size8': struct.unpack_from('<H', raw, 48)[0], 'pal_dict': struct.unpack_from('<I', raw, 52)[0],
            'pal_data': struct.unpack_from('<I', raw, 56)[0]}


def extend_tileset(btx0, textures):
    """Append textures to a stock BTX0 without moving or changing existing texels/palettes.

    ``textures``: [{name, palette_name, width, height, format, color0_transparent,
    indices, palette: [rgb555 words]}]. Returns (bytes, report).
    """
    magic, bom, version, size, header, count = struct.unpack_from('<4sHHIHH', btx0)
    require(magic == b'BTX0' and bom == 0xfeff and count == 1 and size == len(btx0), 'Unsupported tileset container', 'INVALID_ASSET')
    at = struct.unpack_from('<I', btx0, 16)[0]
    tex0 = btx0[at:]
    h = _tex0_header(tex0)
    require(h['comp_size8'] == 0, 'Tilesets with 4x4-compressed textures are not extended', 'UNSUPPORTED_ASSET')
    old_tex, tex_size = read_dictionary(tex0, h['tex_dict'])
    old_pal, _ = read_dictionary(tex0, h['pal_dict'])
    tex_data = bytearray(tex0[h['tex_data']:h['tex_data'] + h['tex_size8'] * 8])
    pal_data = bytearray(tex0[h['pal_data']:h['pal_data'] + h['pal_size8'] * 8])
    require(len(tex_data) == h['tex_size8'] * 8 and len(pal_data) == h['pal_size8'] * 8, 'Truncated tileset data', 'INVALID_ASSET')
    tex_entries, pal_entries = list(old_tex), list(old_pal)
    names = {n for n, _ in old_tex}
    added = []
    for t in textures:
        require(t['name'] not in names, f"Texture name {t['name']} already exists in the tileset", 'NAME_CONFLICT')
        require(t['width'] in SIZES and t['height'] in SIZES and t['format'] in BITS, 'Unsupported texture size/format', 'UNSUPPORTED_ASSET')
        texels = pack_texels(t['indices'], t['width'], t['height'], t['format'])
        while len(tex_data) % 8:
            tex_data.append(0)
        vram = len(tex_data) >> 3
        tex_data.extend(texels)
        params = (vram | int(math.log2(t['width'] // 8)) << 20 | int(math.log2(t['height'] // 8)) << 23
                  | t['format'] << 26 | (1 << 29 if t['color0_transparent'] else 0))
        tex_entries.append((t['name'], struct.pack('<II', params, t['width'] | t['height'] << 11)))
        words = list(t['palette']) + [0] * (-len(t['palette']) % 4)
        require(len(t['palette']) <= (1 << BITS[t['format']]), 'Palette larger than the format allows', 'INVALID_ASSET')
        pal_offset = len(pal_data) >> 3
        pal_data.extend(struct.pack(f'<{len(words)}H', *words))
        pal_entries.append((t['palette_name'], struct.pack('<HH', pal_offset, 0)))
        names.add(t['name']); added.append({'name': t['name'], 'vram8': vram, 'bytes': len(texels), 'palette8': pal_offset})
    require(len(tex_entries) < 256 and len(pal_entries) < 256, 'Tileset dictionaries are full', 'RESOURCE_CAPACITY')
    tdict = dictionary([n for n, _ in tex_entries], [e for _, e in tex_entries])
    pdict = dictionary([n for n, _ in pal_entries], [e for _, e in pal_entries])
    # Stock layout order: header(60) | texture dict | palette dict | texel data | palette data.
    head_len = 60
    require(h['tex_dict'] == head_len, 'Unexpected tileset block order', 'UNSUPPORTED_ASSET')
    tex_dict_off = head_len
    pal_dict_off = tex_dict_off + len(tdict)
    tex_data_off = pal_dict_off + len(pdict)          # 4-byte aligned, as the stock converter
    pal_data_off = tex_data_off + len(tex_data)
    block = bytearray(tex0[:head_len])
    struct.pack_into('<H', block, 12, len(tex_data) >> 3)
    struct.pack_into('<H', block, 14, tex_dict_off)
    struct.pack_into('<I', block, 20, tex_data_off)
    struct.pack_into('<I', block, 36, pal_data_off)      # empty compressed sections follow texel data
    struct.pack_into('<I', block, 40, pal_data_off)
    struct.pack_into('<H', block, 48, len(pal_data) >> 3)
    struct.pack_into('<I', block, 52, pal_dict_off)
    struct.pack_into('<I', block, 56, pal_data_off)
    body = bytes(block) + tdict + pdict
    body += bytes(tex_data_off - len(body)) + bytes(tex_data) + bytes(pal_data)
    body = bytearray(body); struct.pack_into('<I', body, 4, len(body))
    out = bytearray(btx0[:at]) + body
    struct.pack_into('<I', out, 8, len(out))
    return bytes(out), {'added': added, 'texel_bytes': len(tex_data), 'palette_bytes': len(pal_data),
                        'textures': len(tex_entries), 'palettes': len(pal_entries)}


def tileset_budget(btx0):
    at = struct.unpack_from('<I', btx0, 16)[0]
    h = _tex0_header(btx0[at:])
    return {'texel_bytes': h['tex_size8'] * 8, 'compressed_bytes': h['comp_size8'] * 8, 'palette_bytes': h['pal_size8'] * 8}


# ---- model --------------------------------------------------------------------------

# Stock static-prop material words (a/0/4/0 yo_sp1 / h_kage): diffuse 25/31 + vertex
# colour flag, ambient 31, light 0, front faces, fog, opaque alpha 31 (shadow alpha 9).
DIFF_AMB, SPEC_EMI = 0x7fffe739, 0
POLY_MASK, TEX_MASK = 0x3f1ff8ff, 0xffffffff
MATERIAL_FLAGS = 0x1fce


def _polygon_attr(alpha, both_faces, lit):
    return (1 if lit else 0) | (0xc0 if both_faces else 0x80) | 0x8000 | (alpha & 31) << 16


def _material(width, height, alpha, both_faces, lit, repeat):
    return struct.pack('<HH6I4H2i', 0, 44, DIFF_AMB, SPEC_EMI, _polygon_attr(alpha, both_faces, lit), POLY_MASK,
                       (3 << 16) if repeat else 0, TEX_MASK, 0, MATERIAL_FLAGS, width, height, 4096, 4096)


def _fx(v, scale):
    value = round(v / scale * 4096)
    require(-32768 <= value <= 32767, 'Vertex outside the 16-bit range; raise the position scale', 'UNSUPPORTED_ASSET')
    return value


def _normal(n):
    length = math.sqrt(sum(c * c for c in n)) or 1.0
    q = [max(-511, min(511, round(c / length * 511))) for c in n]
    return (q[0] & 0x3ff) | (q[1] & 0x3ff) << 10 | (q[2] & 0x3ff) << 20


def _shape(triangles, scale, material):
    """GPU command list: BEGIN triangles, per vertex NORMAL/TEXCOORD/VTX_16."""
    cmds = [(0x40, [0])]
    w, h = material['width'], material['height']
    for tri in triangles:
        for v in tri['vertices']:
            u, t = v['uv']
            su, sv = round(u * w * 16), round(t * h * 16)
            require(-32768 <= su <= 32767 and -32768 <= sv <= 32767, 'UV outside the 12.4 texel range', 'UNSUPPORTED_ASSET')
            x, y, z = (_fx(c, scale) for c in v['position'])
            cmds += [(0x21, [_normal(v['normal'])]), (0x22, [(su & 0xffff) | (sv & 0xffff) << 16]),
                     (0x23, [(x & 0xffff) | (y & 0xffff) << 16, z & 0xffff])]
    cmds.append((0x41, []))
    while len(cmds) % 4:
        cmds.append((0, []))
    gpu = bytearray()
    for i in range(0, len(cmds), 4):
        packet = cmds[i:i + 4]
        gpu.extend(bytes(op for op, _ in packet))
        for _, params in packet:
            gpu.extend(b''.join(struct.pack('<I', p) for p in params))
    return struct.pack('<HHIII', 0, 16, 5, 16, len(gpu)) + bytes(gpu)   # flag: normals + texcoords


def model(name, materials, shapes, position_scale=None):
    """One static model. materials: [{name, texture, palette, width, height, alpha,
    both_faces, lit, repeat}]; shapes: [{name, material (index), triangles:
    [{vertices: [{position, uv (0..1 of the texture), normal}] * 3}]}]."""
    require(1 <= len(materials) < 16 and 1 <= len(shapes) < 16, 'A prop has 1..15 materials and shapes', 'UNSUPPORTED_ASSET')
    points = [v['position'] for s in shapes for t in s['triangles'] for v in t['vertices']]
    require(points, 'The model has no triangles', 'INVALID_ASSET')
    extent = max(max(abs(c) for p in points for c in p),
                 max(max(p[i] for p in points) - min(p[i] for p in points) for i in range(3)))
    # Positions and the bounding box are fx16 multiples of the position scale.
    scale = position_scale or max(1, 2 ** math.ceil(math.log2(max(extent, 1) / 7.9)))
    ntris = sum(len(s['triangles']) for s in shapes)
    require(ntris <= 512, 'A static prop is limited to 512 triangles', 'RESOURCE_CAPACITY')
    # Node dictionary entry = offset of the node record (flags 7: no translation, rotation
    # or scale), which follows the dictionary as in stock building models.
    node_dict = len(dictionary(['root'], [bytes(4)]))
    nodes = dictionary(['root'], [struct.pack('<I', node_dict)]) + struct.pack('<HH', 7, 0)
    sbc = bytearray([0x26, 0, 0, 0, 0, 0x02, 0, 1, 0x0b])       # NODEDESC identity, NODE visible, POSSCALE
    for i, s in enumerate(shapes):
        sbc.extend([0x04, s['material'], 0x05, i])
    sbc.extend([0x2b, 0x01])
    sbc = bytes(sbc) + bytes(-len(sbc) % 4)
    mat_records = [_material(m['width'], m['height'], m['alpha'], m['both_faces'], m['lit'], m['repeat']) for m in materials]
    mat_dict = dictionary([m['name'] for m in materials], [bytes(4)] * len(materials))
    # material record offsets are relative to the material block start
    base = 4 + len(mat_dict)
    offsets = []
    cursor = base
    for rec in mat_records:
        offsets.append(cursor); cursor += len(rec)
    mat_dict = dictionary([m['name'] for m in materials], [struct.pack('<I', o) for o in offsets])
    textures = sorted({m['texture'] for m in materials})
    palettes = sorted({m['palette'] for m in materials})
    ids_start = cursor
    tex_bind, ids, pal_bind = [], bytearray(), []
    for tname in textures:
        users = [i for i, m in enumerate(materials) if m['texture'] == tname]
        tex_bind.append((len(ids), len(users))); ids.extend(users)
    for pname in palettes:
        users = [i for i, m in enumerate(materials) if m['palette'] == pname]
        pal_bind.append((len(ids), len(users))); ids.extend(users)
    ids = bytes(ids) + bytes(-len(ids) % 4)
    tdict_off = ids_start + len(ids)
    tdict = dictionary(textures, [struct.pack('<HBB', ids_start + a, n, 0) for a, n in tex_bind])
    pdict = dictionary(palettes, [struct.pack('<HBB', ids_start + a, n, 0) for a, n in pal_bind])
    mats = struct.pack('<HH', tdict_off, tdict_off + len(tdict)) + mat_dict + b''.join(mat_records) + ids + tdict + pdict
    blobs = [_shape(s['triangles'], scale, materials[s['material']]) for s in shapes]
    shp_dict = dictionary([s['name'] for s in shapes], [bytes(4)] * len(shapes))
    offsets, cursor = [], len(shp_dict)
    for b in blobs:
        offsets.append(cursor); cursor += len(b)
    shp = dictionary([s['name'] for s in shapes], [struct.pack('<I', o) for o in offsets]) + b''.join(blobs)
    sbc_off = 64 + len(nodes)
    mat_off = sbc_off + len(sbc)
    shp_off = mat_off + len(mats)
    size = shp_off + len(shp)
    q = [[_fx(p[i], scale) for p in points] for i in range(3)]
    mins = [min(c) for c in q]; extents = [max(c) - min(c) for c in q]
    header = struct.pack('<5I8B2I4H6h2I', size, sbc_off, mat_off, shp_off, size, 0, 0, 0, 1, len(materials),
                         len(shapes), 1, 0, scale * 4096, 4096 // scale if scale <= 4096 else 1,
                         ntris * 3, ntris, ntris, 0, *mins, *extents, scale * 4096, 4096 // scale if scale <= 4096 else 1)
    body = header + nodes + sbc + mats + shp
    mdl = dictionary([name], [struct.pack('<I', 8 + len(dictionary([name], [bytes(4)])))]) + body
    mdl0 = b'MDL0' + struct.pack('<I', 8 + len(mdl)) + mdl
    return b'BMD0' + struct.pack('<HHIHHI', 0xfeff, 2, 20 + len(mdl0), 16, 1, 20) + mdl0, {
        'triangles': ntris, 'vertices': ntris * 3, 'position_scale': scale,
        'bounds': [[m * scale / 4096 for m in mins], [(m + e) * scale / 4096 for m, e in zip(mins, extents)]]}


# ---- map-model extension (custom ground materials) ------------------------------------

def _sbc_return(model, start, end):
    """Where new draws go in a stock map-model SBC: before the closing inverse position scale
    (0x2b) that precedes RETURN (0x01), so they draw under the model's position scale."""
    widths = {0x00: 0, 0x02: 2, 0x04: 1, 0x24: 1, 0x44: 1, 0x05: 1, 0x06: 3, 0x26: 4, 0x46: 4, 0x66: 5,
              0x0b: 0, 0x2b: 0}
    cursor, closing = start, None
    while cursor < end:
        command = model[cursor]
        if command == 0x01:
            return cursor if closing is None else closing
        require(command in widths, f'Unsupported map-model scene command 0x{command:02x}', 'UNSUPPORTED_SURFACE')
        closing = cursor if command == 0x2b else (closing if command == 0x00 else None)
        cursor += 1 + widths[command]
    require(False, 'Map-model scene has no return', 'UNSUPPORTED_SURFACE')


def extend_map_model(model, additions):
    """Append materials and shapes to one MDL0 model body (the bytes from its model header).

    ``additions``: [{name, texture, palette, record (a >= 44-byte material record),
    display_list}]. Existing nodes, scene commands, material records, texture/palette
    bindings, shape records and display lists are copied unchanged (display lists may lie
    anywhere in the model, as the surface writer relocates them); new materials bind by
    name to the area tileset like every stock ground material, and each draws one new shape
    under node 0 right before the scene RETURN. Header counts other than the material/shape
    numbers are the caller's (the surface writer updates vertex/polygon totals and box).
    """
    if not additions:
        return model
    size, sbc_off, mat_off, shp_off, env_off = struct.unpack_from('<5I', model)
    require(size == len(model) and env_off in (size, shp_off, 0) and sbc_off < mat_off < shp_off <= size,
            'Unsupported map-model layout', 'UNSUPPORTED_SURFACE')
    num_mat, num_shp = model[24], model[25]
    # Materials.
    mats = model[mat_off:shp_off]
    tdict_at, pdict_at = struct.unpack_from('<HH', mats)
    mat_entries, _ = read_dictionary(mats, 4)
    require(len(mat_entries) == num_mat, 'Material count differs from the model header', 'UNSUPPORTED_SURFACE')
    records = []
    for name, value in mat_entries:
        at = struct.unpack('<I', value)[0]
        tag, rsize = struct.unpack_from('<HH', mats, at)
        require(tag == 0 and rsize >= 44, f'Unsupported material record {name}', 'UNSUPPORTED_SURFACE')
        records.append((name, bytes(mats[at:at + rsize])))
    bindings = {}
    for key, at in (('texture', tdict_at), ('palette', pdict_at)):
        entries, _ = read_dictionary(mats, at)
        bindings[key] = []
        for name, value in entries:
            start, count, flag = struct.unpack('<HBB', value)
            bindings[key].append((name, list(mats[start:start + count]), flag))
    indices = []
    for a in additions:
        # A stock model may keep a material with no shape at its area-animation slot (e.g. sea_on):
        # a new shape of that texture draws with it, so it animates like stock water.
        slot = next((i for i, (n, _) in enumerate(records) if n == a['name']), None)
        bound = lambda key, i: any(b[0] == a[key] and i in b[1] for b in bindings[key])
        if slot is not None and slot < num_mat and bound('texture', slot):
            indices.append(slot)            # keeps that slot's own stock palette binding
            continue
        if (a.get('animate') and slot is not None and slot < num_mat
                and not any(slot in b[1] for key in ('texture', 'palette') for b in bindings[key])):
            # An unbound stock slot kept only so the area texture animation's tracks line up by
            # index (e.g. river_r in outdoor models): it takes the new texture, so the track moves it.
            rec = bytes(a['record'])
            require(len(rec) >= 44 and struct.unpack_from('<HH', rec) == (0, len(rec)), 'Invalid added material record',
                    'INVALID_ASSET')
            records[slot] = (records[slot][0], rec)
            indices.append(slot)
            for key in ('texture', 'palette'):
                found = next((b for b in bindings[key] if b[0] == a[key]), None)
                if found:
                    found[1].append(slot)
                else:
                    bindings[key].append((a[key], [slot], 0))
            continue
        name = a['name']
        if slot is not None:
            taken = {n for n, _ in records}
            name = next(f'{a["name"][:13]}_{i}' for i in range(1, 99) if f'{a["name"][:13]}_{i}' not in taken)
        rec = bytes(a['record'])
        require(len(rec) >= 44 and struct.unpack_from('<HH', rec) == (0, len(rec)), 'Invalid added material record',
                'INVALID_ASSET')
        a = {**a, 'name': name}
        records.append((name, rec))
        index = len(records) - 1
        indices.append(index)
        for key in ('texture', 'palette'):
            found = next((b for b in bindings[key] if b[0] == a[key]), None)
            if found:
                found[1].append(index)
            else:
                bindings[key].append((a[key], [index], 0))
    # Scene: insert "MAT m; SHP s" pairs for the new shapes before RETURN, under node 0.
    at = _sbc_return(model, sbc_off, mat_off)
    extra = bytearray()
    for i, index in enumerate(indices):
        extra.extend([0x04, index, 0x05, num_shp + i])
    tail = model[at:mat_off]
    ret = at + tail.index(1)
    sbc = model[sbc_off:at] + bytes(extra) + model[at:ret + 1]
    sbc += bytes(-len(sbc) % 4)
    require(len(records) < 256, 'Too many materials in one map model', 'RESOURCE_CAPACITY')
    mat_dict = dictionary([n for n, _ in records], [bytes(4)] * len(records))
    cursor, offsets = 4 + len(mat_dict), []
    for _, rec in records:
        offsets.append(cursor)
        cursor += len(rec)
    mat_dict = dictionary([n for n, _ in records], [struct.pack('<I', o) for o in offsets])
    ids = bytearray()
    spans = {}
    for key in ('texture', 'palette'):
        spans[key] = []
        for name, users, flag in bindings[key]:
            spans[key].append((name, cursor + len(ids), len(users), flag))
            ids.extend(users)
    ids = bytes(ids) + bytes(-len(ids) % 4)
    tdict_off = cursor + len(ids)
    tdict = dictionary([s[0] for s in spans['texture']], [struct.pack('<HBB', s[1], s[2], s[3]) for s in spans['texture']])
    pdict = dictionary([s[0] for s in spans['palette']], [struct.pack('<HBB', s[1], s[2], s[3]) for s in spans['palette']])
    new_mats = (struct.pack('<HH', tdict_off, tdict_off + len(tdict)) + mat_dict + b''.join(r for _, r in records)
                + ids + tdict + pdict)
    # Shapes: records then display lists (4-aligned), existing lists copied from wherever they are.
    shp_entries, _ = read_dictionary(model, shp_off)
    require(len(shp_entries) == num_shp, 'Shape count differs from the model header', 'UNSUPPORTED_SURFACE')
    shapes = []
    for name, value in shp_entries:
        pos = shp_off + struct.unpack('<I', value)[0]
        tag, rsize, flags, dl, length = struct.unpack_from('<HHIII', model, pos)
        require(tag == 0 and rsize == 16, f'Unsupported shape record {name}', 'UNSUPPORTED_SURFACE')
        shapes.append((name, flags, bytes(model[pos + dl:pos + dl + length])))
    taken_shapes = {n for n, _, _ in shapes}
    for i, a in enumerate(additions):
        name = f"{a['name']}"[:16]
        if name in taken_shapes:
            name = next(f'{a["name"][:12]}_s{k}' for k in range(1, 99) if f'{a["name"][:12]}_s{k}' not in taken_shapes)
        taken_shapes.add(name)
        shapes.append((name, 0, bytes(a['display_list'])))
    require(len(shapes) < 256, 'Too many shapes in one map model', 'RESOURCE_CAPACITY')
    names = [n for n, _, _ in shapes]
    require(len(set(names)) == len(names), 'Shape names must be unique', 'NAME_CONFLICT')
    shp_dict_len = len(dictionary(names, [bytes(4)] * len(shapes)))
    rec_start = shp_dict_len
    lists_start = rec_start + 16 * len(shapes)
    recs, lists, offsets = bytearray(), bytearray(), []
    for i, (name, flags, dl) in enumerate(shapes):
        pos = rec_start + 16 * i
        offsets.append(pos)
        dl_at = lists_start + len(lists)
        recs.extend(struct.pack('<HHIII', 0, 16, flags, dl_at - pos, len(dl)))
        lists.extend(dl)
        lists.extend(bytes(-len(lists) % 4))
    new_shp = dictionary(names, [struct.pack('<I', o) for o in offsets]) + bytes(recs) + bytes(lists)
    nodes = model[64:sbc_off]
    head = bytearray(model[:64])
    new_sbc_off = 64 + len(nodes)
    new_mat_off = new_sbc_off + len(sbc)
    new_shp_off = new_mat_off + len(new_mats)
    new_size = new_shp_off + len(new_shp)
    struct.pack_into('<5I', head, 0, new_size, new_sbc_off, new_mat_off, new_shp_off,
                     new_size if env_off in (size, 0) else new_shp_off)
    head[24], head[25] = len(records), len(shapes)
    return bytes(head) + bytes(nodes) + sbc + new_mats + new_shp


def map_material_record(template, width, height, repeat=(True, True), alpha=None):
    """A ground material record cloned from a stock map material (``template`` bytes):
    same lighting/diffuse words, new texture size, repeat flags and optional polygon alpha."""
    rec = bytearray(template[:44])
    struct.pack_into('<HH', rec, 0, 0, 44)
    polygon = struct.unpack_from('<I', rec, 12)[0]
    if alpha is not None:
        polygon = (polygon & ~(31 << 16)) | (alpha & 31) << 16
    struct.pack_into('<I', rec, 12, polygon)
    params = struct.unpack_from('<I', rec, 20)[0] & ~(0xF << 16)
    params |= (1 << 16 if repeat[0] else 0) | (1 << 17 if repeat[1] else 0)
    struct.pack_into('<I', rec, 20, params)
    flags = struct.unpack_from('<H', rec, 30)[0] | 0x0E        # texture matrix: scale one, rotation/translation zero
    struct.pack_into('<H', rec, 30, flags)
    struct.pack_into('<HH2i', rec, 32, width, height, 4096, 4096)
    return bytes(rec)
