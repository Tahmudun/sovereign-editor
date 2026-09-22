"""Read the static Nitro model subset used by the stock scene and by map contexts.

No model writer. ndspy 4.1 supplies texture decoding and command parameter counts.
Unknown scene commands, node transforms and material transforms fail explicitly.

Map terrain models (the ``a/0/6/5`` model section) and most building models carry
no embedded ``TEX0``: their textures live in the area's tileset (``a/0/4/4`` for
terrain, ``a/0/7/0`` for buildings). ``decode_model`` therefore accepts an external
texture block; nothing is guessed or substituted when a name is missing.
"""
from dataclasses import dataclass
from functools import lru_cache
import struct

import ndspy._common
import ndspy.color
import ndspy.model
import ndspy.texture
import numpy as np

from .formats import require, span


@dataclass
class Primitive:
    vertices: np.ndarray
    colors: np.ndarray
    uvs: np.ndarray
    triangles: np.ndarray
    material: dict
    texture: np.ndarray | None = None
    normals: np.ndarray | None = None
    polygons: list | None = None
    shade_commands: np.ndarray | None = None


def info(data, offset, width=4):
    size = struct.unpack_from("<H", span(data, offset, 4), 2)[0]
    block = span(data, offset, size)
    try:
        entries = ndspy._common.loadInfoBlock(block, 0, width)
    except (AssertionError, struct.error, IndexError) as exc:
        require(False, f"Invalid Nitro dictionary: {exc}")
    require(all(len(value) == width for _, _, _, value in entries), "Truncated Nitro dictionary")
    return [(name, value) for name, _, _, value in entries]


def blocks(raw):
    magic, bom, version, size, header, count = struct.unpack("<4sHHIHH", span(raw, 0, 16))
    require(magic in (b"BMD0", b"BTX0") and bom == 0xfeff and version in (1, 2)
            and size == len(raw) and header == 16 and 1 <= count <= 2, "Unsupported Nitro container")
    result = {}
    for offset in struct.unpack(f"<{count}I", span(raw, 16, count * 4)):
        kind, size = struct.unpack("<4sI", span(raw, offset, 8))
        require(kind in (b"MDL0", b"TEX0") and kind not in result and size >= 8, "Unsupported Nitro block")
        result[kind] = span(raw, offset, size)
    return result


def _nodes(model):
    """NNS node transforms: translation, pivot or full 3x3 rotation, and scale.

    Field order follows the Nitro node block: ``flags``/``m00``, then translation
    when bit 0 is clear, the rotation when bit 1 is clear (pivot form when bit 3 is
    set, otherwise the remaining eight 3x3 elements), then scale and its inverse
    when bit 2 is clear. Every decoded transform is checked against the model's own
    bounding box in :func:`decode_model`, so a misread matrix fails loudly.

    The hardware multiplies row vectors (``v * M``), so a stored rotation is the
    transpose of the column-vector matrix used here. ``wk_sp1`` (building model 27)
    carries both an asymmetric pivot node and a full 3x3 node, and only the
    transposed reading reproduces its stored bounding box (0.004 of a unit).
    """
    result = []
    for name, value in info(model, 64):
        offset = 64 + struct.unpack("<I", value)[0]
        flags, m00 = struct.unpack("<Hh", span(model, offset, 4))
        cursor = offset + 4
        matrix = np.eye(4)
        if not flags & 1:
            matrix[:3, 3] = np.array(struct.unpack("<3i", span(model, cursor, 12))) / 4096
            cursor += 12
        if not flags & 2:
            if flags & 8:
                # Pivot form: one axis is kept and two signed elements are stored.
                pivot_axis = (flags >> 4) & 15
                require(pivot_axis in (0, 4, 8), f"Unsupported node pivot axis: {name}")
                a, b = np.array(struct.unpack("<2h", span(model, cursor, 4))) / 4096
                cursor += 4
                pivot = -1 if flags & 0x100 else 1
                c = -b if flags & 0x200 else b
                d = -a if flags & 0x400 else a
                rotation = np.zeros((3, 3))
                # The kept axis is the row/column the pivot value sits on.
                keep = pivot_axis // 4
                others = [i for i in range(3) if i != keep]
                rotation[keep, keep] = pivot
                rotation[others[0], others[0]], rotation[others[0], others[1]] = a, b
                rotation[others[1], others[0]], rotation[others[1], others[1]] = c, d
                matrix[:3, :3] = rotation.T
            else:
                rest = np.array(struct.unpack("<8h", span(model, cursor, 16))) / 4096
                cursor += 16
                matrix[:3, :3] = np.insert(rest, 0, m00 / 4096).reshape(3, 3).T
        if not flags & 4:
            scale = np.array(struct.unpack("<3i", span(model, cursor, 12))) / 4096
            inverse = np.array(struct.unpack("<3i", span(model, cursor + 12, 12))) / 4096
            cursor += 24
            require(np.all(scale != 0) and np.allclose(scale * inverse, 1, atol=1e-3),
                    f"Node scale disagrees with its stored inverse: {name}")
            matrix[:3, :3] = matrix[:3, :3] * scale
        result.append(matrix)
    return result


# NNS_G3D_MATFLAG bits that follow the fixed 44-byte material record. A clear bit
# means the corresponding texture-matrix field is stored after it.
TEXMTX_SCALE_ONE, TEXMTX_ROT_ZERO, TEXMTX_TRANS_ZERO = 2, 4, 8


def _materials(model, offset, geometry_only=False):
    texture_dict, palette_dict = struct.unpack("<HH", span(model, offset, 4))
    result = []
    for name, value in info(model, offset + 4):
        p = offset + struct.unpack("<I", value)[0]
        tag, size = struct.unpack("<HH", span(model, p, 4))
        require(tag == 0 and size >= 44, f"Unsupported material transform: {name}")
        raw = span(model, p, size)
        diffuse, specular, polygon, mask, texparams, texmask = struct.unpack_from("<6I", raw, 4)
        _, flags, width, height, mag_s, mag_t = struct.unpack_from("<4H2i", raw, 28)
        cursor, uv_scale, uv_translate = 44, [1.0, 1.0], [0.0, 0.0]
        if not geometry_only:
            def stored(fmt, what):
                nonlocal cursor
                need = struct.calcsize(fmt)
                require(cursor + need <= size,
                        f"Truncated material {what}: {name} declares {size} bytes, needs {cursor + need}")
                values = struct.unpack_from(fmt, raw, cursor)
                cursor += need
                return [v / 4096 for v in values]

            if not flags & TEXMTX_SCALE_ONE:
                uv_scale = stored("<2i", "texture-matrix scale")
            # A stored sine/cosine pair would rotate texture space; no map or building
            # model of the pinned ROM uses it, so it is reported instead of guessed.
            require(flags & TEXMTX_ROT_ZERO, f"Unsupported texture-matrix rotation: {name}")
            if not flags & TEXMTX_TRANS_ZERO:
                uv_translate = stored("<2i", "texture-matrix translation")
            require(cursor == size, f"Unsupported material transform: {name} has {size - cursor} "
                                    "unexplained trailing bytes")
            require(mag_s == mag_t == 4096, f"Unsupported texture magnification: {name}")
            # The field layout is exact (a 52-byte record is 44 bytes plus the stored
            # translation pair), but no source available here fixes the units or sign
            # these values take in texture space. A non-identity value is therefore
            # refused by name instead of being applied with a guessed convention.
            require(uv_scale == [1.0, 1.0] and uv_translate == [0.0, 0.0],
                    f"Unsupported texture-matrix transform: {name} stores scale {uv_scale} and "
                    f"translation {uv_translate} texels; their convention is not verified")
        result.append({"name": name, "diffuse": [(diffuse >> shift & 31) / 31 for shift in (0, 5, 10)],
                       "set_vertex_color": bool(diffuse & 0x8000), "alpha": (polygon >> 16 & 31) / 31,
                       "width": width, "height": height, "repeat": [bool(texparams & (1 << i)) for i in (16, 17)],
                       "mirror": [bool(texparams & (1 << i)) for i in (18, 19)],
                       "uv_scale": uv_scale, "uv_translate": uv_translate, "flags": flags,
                       "texture_name": None, "palette_name": None})
    for key, dictionary in (("texture_name", texture_dict), ("palette_name", palette_dict)):
        for name, value in info(model, offset + dictionary):
            start, count, _ = struct.unpack("<HBB", value)
            for mat in span(model, offset + start, count):
                require(mat < len(result) and result[mat][key] is None, "Ambiguous material binding")
                result[mat][key] = name
    return result


def _draw_order(model, sbc, end, nodes):
    cursor, node, material, scaled = sbc, None, None, False
    matrices, draws = {}, []
    while cursor < end:
        command = model[cursor]
        cursor += 1
        kind = command & 31
        if command == 0:
            continue
        if command == 1:
            require(not any(model[cursor:end]), "Unexpected data after scene return")
            return draws
        if kind == 6:  # NODEDESC with optional matrix store/restore.
            require(command in (6, 0x26), "Unsupported node hierarchy command")
            idx, parent, flags = span(model, cursor, 3)
            cursor += 3
            require(idx < len(nodes) and flags == 0, "Unsupported node hierarchy")
            require(parent == idx or parent in matrices, "Unresolved parent node")
            matrix = nodes[idx] if parent == idx else matrices[parent] @ nodes[idx]
            matrices[idx] = matrix
            if command & 0x20:
                slot = span(model, cursor, 1)[0]
                cursor += 1
                require(slot < 32, "Invalid node matrix stack slot")
        elif command == 2:
            node, visible = span(model, cursor, 2)
            cursor += 2
            require(node in matrices and visible == 1, "Unsupported node visibility")
        elif kind == 4:
            require(command in (4, 0x24, 0x44), "Unsupported material command")
            material = span(model, cursor, 1)[0]
            cursor += 1
        elif command == 5:
            shape = span(model, cursor, 1)[0]
            cursor += 1
            require(node in matrices and material is not None and scaled, "Unqualified shape transform")
            draws.append((shape, material, matrices[node].copy()))
        elif command in (0x0b, 0x2b):
            scaled = command == 0x0b
        else:
            require(False, f"Unsupported Nitro scene command 0x{command:02x}")
    require(False, "Nitro scene has no return")


def _signed(value, bits):
    return value - (1 << bits) if value & (1 << (bits - 1)) else value


def _display_list(data, scale, matrix, material):
    cursor, mode = 0, None
    last, uv = np.zeros(3), np.zeros(2)
    color = np.array(material["diffuse"] if material["set_vertex_color"] else [1, 1, 1])
    vertices, colors, uvs, triangles, group, normals = [], [], [], [], [], []
    normal = 0
    shade = 0x20 if material['set_vertex_color'] else 0
    shades, polygons = [], []
    while cursor < len(data):
        commands = span(data, cursor, 4)
        cursor += 4
        for command in commands:
            require(command in (0, 0x20, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x40, 0x41),
                    f"Unsupported geometry command 0x{command:02x}")
            count = ndspy.model._COMMANDS_BY_ID[command].PARAM_COUNT
            raw = span(data, cursor, count * 4)
            cursor += count * 4
            if command == 0:
                continue
            v = struct.unpack_from("<I", raw)[0] if count else 0
            if command == 0x20:
                color = np.array([(v >> shift & 31) / 31 for shift in (0, 5, 10)])
                shade = command
            elif command == 0x21:
                # Normals are retained by the source. This inspection renderer
                # uses unlit textures/vertex colors, not the game's dynamic lights.
                normal = v
                shade = command
            elif command == 0x22:
                uv = np.array(struct.unpack("<2h", raw)) / 16
            elif 0x23 <= command <= 0x28:
                if command == 0x23:
                    last = np.array(struct.unpack_from("<3h", raw)) / 4096
                elif command == 0x24:
                    last = np.array([_signed(v >> shift & 1023, 10) for shift in (0, 10, 20)]) / 64
                elif command == 0x28:
                    last += np.array([_signed(v >> shift & 1023, 10) for shift in (0, 10, 20)]) / 4096
                else:
                    axes = {0x25: [0, 1], 0x26: [0, 2], 0x27: [1, 2]}[command]
                    last[axes] = np.array(struct.unpack("<2h", raw)) / 4096
                require(mode is not None, "Vertex outside primitive")
                vertices.append((matrix @ np.append(last * scale, 1))[:3])
                colors.append(color.copy())
                uvs.append(uv.copy())
                normals.append(normal)
                shades.append(shade)
                group.append(len(vertices) - 1)
            elif command == 0x40:
                require(mode is None and v in (0, 1, 2, 3), "Invalid primitive begin")
                mode, group = v, []
            elif command == 0x41:
                require(mode is not None, "Primitive end without begin")
                if mode in (0, 1):
                    stride = 3 if mode == 0 else 4
                    require(len(group) % stride == 0, "Incomplete primitive")
                    for i in range(0, len(group), stride):
                        polygons.append(group[i:i + stride])
                        triangles.append([group[i], group[i + 1], group[i + 2]])
                        if mode == 1:
                            triangles.append([group[i], group[i + 2], group[i + 3]])
                elif mode == 2:
                    for i in range(len(group) - 2):
                        polygons.append([group[i + (i % 2)], group[i + 1 - (i % 2)], group[i + 2]])
                        triangles.append([group[i + (i % 2)], group[i + 1 - (i % 2)], group[i + 2]])
                else:
                    require(len(group) % 2 == 0, "Incomplete quad strip")
                    for i in range(0, len(group) - 2, 2):
                        polygons.append([group[i], group[i + 1], group[i + 3], group[i + 2]])
                        triangles.extend([[group[i], group[i + 1], group[i + 3]],
                                          [group[i], group[i + 3], group[i + 2]]])
                mode = None
    require(mode is None and vertices, "Incomplete or empty display list")
    return Primitive(np.array(vertices), np.array(colors), np.array(uvs), np.array(triangles), material,
                     normals=np.array(normals, dtype=np.uint32), polygons=polygons,
                     shade_commands=np.array(shades, dtype=np.uint8))


def texture_set(raw):
    """Decoded ``TEX0`` name tables of an NSBTX/NSBMD container; nothing is renamed.

    ndspy 4.1 derives each palette's length from the *next* palette's start offset,
    which yields an empty palette whenever two entries legitimately share one start
    (the area tilesets do this). The palette bounds are therefore recomputed here
    from the block's own palette-data range; the texture tables are ndspy's.
    """
    container = blocks(raw)
    require(b"TEX0" in container, "Texture block absent")
    data = container[b"TEX0"]
    require(len(data) >= 0x3C, "Truncated TEX0 header")
    if not hasattr(ndspy.color, "LUT_UNPACKED"):
        ndspy.color.prepareLUTs()
    try:
        decoded = ndspy.texture._readTEX0(data)
    except (AssertionError, struct.error, IndexError, ValueError) as exc:
        require(False, f"Invalid TEX0 texture tables: {exc}")
    textures = dict(decoded["textures"])
    require(len(textures) == len(decoded["textures"]), "Duplicate texture names")
    palette_bytes, palette_dict, palette_data = struct.unpack_from("<H2xII", data, 0x30)
    palette_bytes <<= 3
    palette_end = palette_data + palette_bytes
    require(palette_end <= len(data), "Palette data exceeds its TEX0 block")
    entries = []
    for name, value in info(data, palette_dict, 4):
        start = palette_data + (struct.unpack("<H2x", value)[0] << 3)
        # A palette must start inside the data range; one starting exactly at its
        # end would have no colours and no following boundary.
        require(palette_data <= start < palette_end, f"Palette outside TEX0 palette data: {name}")
        entries.append((name, start))
    bounds = sorted({start for _, start in entries} | {palette_data + palette_bytes})
    palettes = {}
    for name, start in entries:
        end = bounds[bounds.index(start) + 1]
        require(name not in palettes, "Duplicate palette names")
        palettes[name] = ndspy.color.loadPalette(span(data, start, end - start))
    return textures, palettes


def model_names(raw):
    """Internal model names of a container, in dictionary order."""
    container = blocks(raw)
    require(b"MDL0" in container, "Model block absent")
    return [name for name, _ in info(container[b"MDL0"], 8)]


@lru_cache(maxsize=48)
def decode_model(raw, geometry_only=False, tileset=None, index=0):
    """Decode a static model.

    ``geometry_only`` is for independent GLB comparisons. ``tileset`` supplies the
    area tileset bytes (``a/0/4/4`` or ``a/0/7/0``) for the models that carry no
    embedded ``TEX0``; an embedded block always wins, and a name that neither source
    defines is reported rather than substituted. ``index`` selects one model of a
    multi-model container.
    """
    container = blocks(raw)
    require(b"MDL0" in container, "Model block absent")
    data = container[b"MDL0"]
    names = info(data, 8)
    require(type(index) is int and 0 <= index < len(names),
            f"Model {index} is absent from this container ({len(names)} model(s))", "NOT_FOUND")
    name, value = names[index]
    off = struct.unpack("<I", value)[0]
    size, sbc, mat, shape, end = struct.unpack("<5I", span(data, off, 20))
    model = span(data, off, size)
    require(64 <= sbc < mat < shape < end == size, "Invalid model sections")
    scale, inverse = np.array(struct.unpack_from("<2i", model, 28)) / 4096
    require(scale > 0 and scale * inverse == 1, "Unsupported model position scale")
    nodes = _nodes(model)
    materials = _materials(model, mat, geometry_only)
    shapes = info(model, shape)
    require(list(model[23:26]) == [len(nodes), len(materials), len(shapes)], "Model counts disagree")
    textures, palettes, texture_source = {}, {}, None
    if not geometry_only:
        if b"TEX0" in container:
            textures, palettes = texture_set(raw)
            texture_source = "embedded TEX0"
        needed = {m["texture_name"] for m in materials if m["texture_name"] is not None}
        if needed - set(textures):
            require(tileset is not None,
                    f"Textures absent for {name}: no embedded TEX0 and no area tileset supplied")
            external, external_palettes = texture_set(tileset)
            textures, palettes = {**external, **textures}, {**external_palettes, **palettes}
            texture_source = "area tileset" if texture_source is None else "embedded TEX0 + area tileset"
    primitives = []
    draws = _draw_order(model, sbc, mat, nodes)
    require(sorted(sid for sid, _, _ in draws) == list(range(len(shapes))), "Incomplete or duplicate shape coverage")
    for sid, mid, matrix in draws:
        require(sid < len(shapes) and mid < len(materials), "Invalid shape/material index")
        label, value = shapes[sid]
        p = shape + struct.unpack("<I", value)[0]
        _, _, dl, length = struct.unpack("<4I", span(model, p, 16))
        material = dict(materials[mid], shape=label)
        primitive = _display_list(span(model, p + dl, length), scale, matrix, material)
        if not geometry_only and material["texture_name"] is not None:
            tn, pn = material["texture_name"], material["palette_name"]
            require(tn in textures and (pn is None or pn in palettes), f"Unresolved texture/palette: {tn}/{pn}")
            texture = textures[tn]
            require((texture.width, texture.height) == (material["width"], material["height"]), "Texture dimensions disagree")
            # ndspy 4.1 Palette stores ColorTuple entries but its texture renderer
            # indexes a packed-color LUT. Adapt at the boundary without mutating it.
            palette = [ndspy.color.pack(*color) for color in palettes[pn]] if pn else None
            primitive.texture = np.asarray(ndspy.texture.renderTextureDataAsImage(
                texture.data1, texture.data2, texture.format, texture.width, texture.height,
                palette, texture.isColor0Transparent).convert("RGBA"))
        if material["texture_name"] is None or not (material["width"] and material["height"]):
            # An untextured material shades its vertex colors; keep its UVs inert
            # rather than dividing by a zero texture size.
            primitive.uvs = np.zeros_like(primitive.uvs)
        else:
            # Display-list UVs are texels; only identity texture matrices reach here.
            primitive.uvs = primitive.uvs / [material["width"], material["height"]]
        primitives.append(primitive)
    require(len(primitives) == len(shapes), "Incomplete shape coverage")
    expected_vertices, surfaces, tris, quads = struct.unpack_from("<4H", model, 36)
    require(sum(len(p.vertices) for p in primitives) == expected_vertices, "Vertex count disagrees with model")
    require(sum(len(p.triangles) for p in primitives) == tris + 2 * quads, "Triangle count disagrees with model")
    all_vertices = np.concatenate([p.vertices for p in primitives])
    bounds = np.array([all_vertices.min(0), all_vertices.max(0)])
    # Independent native bounding box metadata checks node translations/rotations
    # as well as model position scale. Allow fixed-point rounding of both ends.
    box_scale = struct.unpack_from("<i", model, 56)[0] / 4096
    require(box_scale > 0, "Invalid native bounding box scale")
    quantum = box_scale / 4096
    box = np.array(struct.unpack_from("<6h", model, 44)) * quantum
    native_bounds = np.array([box[:3], box[:3] + box[3:]])
    require(np.max(np.abs(bounds - native_bounds)) <= 2 * quantum,
            f"Decoded geometry disagrees with native bounds: decoded {np.round(bounds, 3).tolist()} "
            f"vs stored {np.round(native_bounds, 3).tolist()}")
    summary = {"name": name, "vertices": expected_vertices, "triangles": tris + 2 * quads,
               "shapes": len(shapes), "nodes": len(nodes), "position_scale": float(scale),
               "bounds": bounds.tolist(), "native_bounds": native_bounds.tolist(), "bounds_quantum": quantum,
               "texture_source": texture_source, "model_index": index, "model_count": len(names),
               "textured_materials": sum(m["texture_name"] is not None for m in materials),
               "materials": materials}
    return summary, primitives
