"""Generic HGSS map-context resolution: headers, matrices, area data and map cells.

Format layout adapted from DSPRE (AGPL-3.0), pinned commit
249a278186d80c35f04485b2c1b612bd81b90a74, see ``references/dspre``:

* ``DS_Map/ROMFiles/MapHeader.cs`` — ``HeaderHGSS`` field order and bit packing.
* ``DS_Map/ROMFiles/GameMatrix.cs`` — matrix width/height, optional header and
  altitude sections, map-member grid and the ``EMPTY`` sentinel.
* ``DS_Map/ROMFiles/AreaData.cs`` — HGSS building/map tileset, area and light fields.
* ``DS_Map/ROMFiles/MapFile.cs`` — section lengths, HGSS BGS block and the
  32x32 type/collision pairs (implemented in :mod:`sovereign_editor.formats`).
* ``DS_Map/RomInfo.cs`` — HGSS archive paths and the US ARM9 header-table offset.

Nothing here writes to a ROM. ``core.Project`` owns every authored write, and the
DSPRE retail executable patches are deliberately NOT applied to the custom ROM.
"""
import struct

from .formats import (arm9_code, digest, map_sections, member_count, require,
                      resource, span)

MAP_ARCHIVE = "a/0/6/5"
MATRIX_ARCHIVE = "a/0/4/1"
AREA_ARCHIVE = "a/0/4/2"
EVENT_ARCHIVE = "a/0/3/2"
MAP_TEXTURE_ARCHIVE = "a/0/4/4"
BUILDING_TEXTURE_ARCHIVE = "a/0/7/0"
BUILDING_MODEL_ARCHIVE = "a/0/4/0"
INTERIOR_MODEL_ARCHIVE = "a/1/4/8"
NAME_TABLE = "fielddata/maptable/mapname.bin"

# RomInfo.cs SetHeaderTableOffset(): US HeartGold/SoulSilver, decompressed ARM9.
HEADER_TABLE = 0xF6BE0
HEADER_SIZE = 24
NAME_LENGTH = 16
MAP_SIZE = 32
EMPTY = 0xFFFF
AREA_TYPES = {0: "indoor", 1: "outdoor"}


def header_count(blob):
    """Header count from the internal-name table; never a hardcoded map list."""
    try:
        _, raw = span_named(blob, NAME_TABLE)
    except Exception:  # pragma: no cover - profile guard covers the US ROM
        return 0
    require(len(raw) % NAME_LENGTH == 0, "Unsupported internal map-name table")
    return len(raw) // NAME_LENGTH


def span_named(blob, name):
    from .formats import file_span
    return file_span(blob, name)


def header_name(blob, header_id):
    _, raw = span_named(blob, NAME_TABLE)
    chunk = span(raw, header_id * NAME_LENGTH, NAME_LENGTH)
    return chunk.split(b"\x00")[0].decode("ascii", errors="replace").strip()


def read_header(blob, header_id, arm9=None):
    """HeaderHGSS: 24 bytes at ``HEADER_TABLE + id * 24`` of the decompressed ARM9."""
    code = arm9_code(blob) if arm9 is None else arm9
    count = header_count(blob)
    require(type(header_id) is int and 0 <= header_id < count,
            f"Map header {header_id} is outside this ROM's header table (0..{count - 1})", "NOT_FOUND")
    offset = HEADER_TABLE + header_id * HEADER_SIZE
    raw = span(code, offset, HEADER_SIZE)
    (wild, area_data, coords, matrix, script, level_script, text_archive,
     music_day, music_night, event_file, location_name, area_properties, last32) = struct.unpack("<BBH7HBBI", raw)
    return {"id": header_id, "name": header_name(blob, header_id), "hex": raw.hex(),
            "arm9_offset": offset, "wild_pokemon": wild, "area_data": area_data,
            "worldmap": {"unknown0": coords & 0xF, "x": (coords >> 4) & 0x3F, "y": (coords >> 10) & 0x3F},
            "matrix": matrix, "script_file": script, "level_script": level_script,
            "text_archive": text_archive, "music_day": music_day, "music_night": music_night,
            "event_file": event_file, "location_name": location_name,
            "area_icon": area_properties & 0xF, "mom_call_intro": (area_properties >> 4) & 0xF,
            "kanto": bool(last32 & 1), "weather": (last32 >> 1) & 0x7F,
            "location_type": (last32 >> 8) & 0xF, "camera_angle": (last32 >> 12) & 0x3F,
            "follow_mode": (last32 >> 18) & 0x3, "battle_background": (last32 >> 20) & 0x1F,
            "flags": (last32 >> 25) & 0x7F}


def read_matrix(blob, matrix_id):
    """GameMatrix.cs: width, height, optional header/altitude sections, map grid."""
    require(type(matrix_id) is int and 0 <= matrix_id < member_count(blob, MATRIX_ARCHIVE),
            f"Matrix {matrix_id} is absent from {MATRIX_ARCHIVE}", "NOT_FOUND")
    base, raw = resource(blob, MATRIX_ARCHIVE, matrix_id)
    return decode_matrix(raw, matrix_id, base)


def decode_matrix(raw, matrix_id, base=-1):
    width, height, has_headers, has_heights, name_length = span(raw, 0, 5)
    require(width > 0 and height > 0 and has_headers < 2 and has_heights < 2, "Unsupported matrix header")
    cursor = 5 + name_length
    name = span(raw, 5, name_length).decode("utf-8", errors="replace")
    headers = altitudes = None
    if has_headers:
        headers = [list(struct.unpack_from(f"<{width}H", span(raw, cursor + 2 * width * row, 2 * width)))
                   for row in range(height)]
        cursor += 2 * width * height
    if has_heights:
        altitudes = [list(span(raw, cursor + width * row, width)) for row in range(height)]
        cursor += width * height
    maps = [list(struct.unpack_from(f"<{width}H", span(raw, cursor + 2 * width * row, 2 * width)))
            for row in range(height)]
    cursor += 2 * width * height
    require(cursor == len(raw), "Unexpected trailing matrix data")
    return {"id": matrix_id, "name": name, "width": width, "height": height,
            "has_headers": bool(has_headers), "has_altitudes": bool(has_heights),
            "headers": headers, "altitudes": altitudes, "maps": maps,
            "rom_offset": base, "sha256": digest(raw)}


def read_area_data(blob, area_id):
    """AreaData.cs, HGSS branch: 8 bytes."""
    require(type(area_id) is int and 0 <= area_id < member_count(blob, AREA_ARCHIVE),
            f"Area data {area_id} is absent from {AREA_ARCHIVE}", "NOT_FOUND")
    _, raw = resource(blob, AREA_ARCHIVE, area_id)
    require(len(raw) == 8, "Unsupported HGSS area data length")
    buildings_tileset, map_tileset, dynamic_texture = struct.unpack_from("<3H", raw)
    return {"id": area_id, "buildings_tileset": buildings_tileset, "map_tileset": map_tileset,
            "dynamic_texture_type": dynamic_texture, "area_type": raw[6],
            "area_type_name": AREA_TYPES.get(raw[6], "unknown"), "light_type": raw[7],
            "sha256": digest(raw)}


def matrix_cells(matrix, map_member=None):
    """Every populated cell; a map member may legitimately appear more than once."""
    cells = []
    for y in range(matrix["height"]):
        for x in range(matrix["width"]):
            member = matrix["maps"][y][x]
            if member == EMPTY or (map_member is not None and member != map_member):
                continue
            cells.append({"cell": [x, y], "map_member": member,
                          "header": matrix["headers"][y][x] if matrix["has_headers"] else None,
                          "altitude": matrix["altitudes"][y][x] if matrix["has_altitudes"] else None,
                          "origin": [x * MAP_SIZE, y * MAP_SIZE]})
    return cells


def resolve_context(blob, header=None, matrix=None, cell=None, arm9=None,
                    matrix_reader=None, map_reader=None):
    """Explicit context: resource member plus matrix-cell origin.

    Either a header id (whose matrix is followed) or an explicit matrix id must be
    given. A cell is required whenever the matrix holds more than one populated
    cell for the requested header, because the same map member can be reused.
    """
    require(header is not None or matrix is not None,
            "Choose a map context by header id or matrix id", "CONTEXT_REQUIRED")
    head = read_header(blob, header, arm9) if header is not None else None
    matrix_id = head["matrix"] if head is not None else matrix
    require(matrix is None or head is None or matrix == matrix_id,
            f"Header {header} uses matrix {matrix_id}, not {matrix}", "CONTEXT_MISMATCH")
    grid = matrix_reader(matrix_id) if matrix_reader else read_matrix(blob, matrix_id)
    candidates = matrix_cells(grid)
    if head is not None and grid["has_headers"]:
        candidates = [c for c in candidates if c["header"] == header]
        require(candidates, f"Matrix {matrix_id} has no cell for header {header}", "NOT_FOUND")
    if cell is None:
        require(len(candidates) == 1,
                f"Matrix {matrix_id} has {len(candidates)} candidate cells; pass an explicit cell",
                "CONTEXT_AMBIGUOUS")
        chosen = candidates[0]
    else:
        cell = list(cell)
        require(len(cell) == 2 and all(type(v) is int for v in cell), "Cell must be two integers")
        matches = [c for c in candidates if c["cell"] == cell]
        require(matches, f"Matrix {matrix_id} cell {cell} is empty or outside the requested header", "NOT_FOUND")
        chosen = matches[0]
    header_id = header if header is not None else chosen["header"]
    require(header_id is not None,
            f"Matrix {matrix_id} carries no header section; pass an explicit header id", "CONTEXT_AMBIGUOUS")
    head = head or read_header(blob, header_id, arm9)
    require(head["matrix"] == matrix_id, "Header/matrix disagreement", "CONTEXT_MISMATCH")
    member = chosen["map_member"]
    if map_reader:
        raw = map_reader(member)
        rom_offset = resource(blob, MAP_ARCHIVE, member)[0] if member < member_count(blob, MAP_ARCHIVE) else -1
    else:
        require(member < member_count(blob, MAP_ARCHIVE), f"Map member {member} is absent from {MAP_ARCHIVE}")
        rom_offset, raw = resource(blob, MAP_ARCHIVE, member)
    sections = map_sections(raw)
    area = read_area_data(blob, head["area_data"])
    return {"id": f"h{head['id']}/m{matrix_id}/c{chosen['cell'][0]},{chosen['cell'][1]}",
            "name": head["name"], "header": head, "area_data": area,
            "matrix": {k: grid[k] for k in ("id", "name", "width", "height", "has_headers", "has_altitudes", "sha256")},
            "cell": {"x": chosen["cell"][0], "y": chosen["cell"][1], "altitude": chosen["altitude"]},
            "origin": chosen["origin"], "map_member": member, "map_archive": MAP_ARCHIVE,
            "map_rom_offset": rom_offset, "map_sha256": digest(raw), "map_bytes": len(raw),
            "sections": sections, "event_member": head["event_file"],
            "shared_cells": [c["cell"] for c in matrix_cells(grid, member)],
            "resources": {"map_textures": {"archive": MAP_TEXTURE_ARCHIVE, "member": area["map_tileset"]},
                          "building_textures": {"archive": BUILDING_TEXTURE_ARCHIVE, "member": area["buildings_tileset"]},
                          "building_models": {"archive": INTERIOR_MODEL_ARCHIVE if area["area_type"] == 0
                                              else BUILDING_MODEL_ARCHIVE},
                          "events": {"archive": EVENT_ARCHIVE, "member": head["event_file"]}}}


def cell_offset(context, x, z):
    """Global tile -> byte offset of the (type, collision) pair inside the map member."""
    ox, oz = context["origin"]
    lx, lz = x - ox, z - oz
    require(type(x) is int and type(z) is int, "Permission cells are integer tiles")
    require(0 <= lx < MAP_SIZE and 0 <= lz < MAP_SIZE,
            f"Tile {x},{z} is outside map cell {context['cell']['x']},{context['cell']['y']}", "OUTSIDE_MAP")
    return context["sections"]["permissions_offset"] + 2 * (lz * MAP_SIZE + lx)


def offset_cell(context, offset):
    """Byte offset inside the map member -> the global tile of one matrix cell."""
    start = context["sections"]["permissions_offset"]
    index = (offset - start) // 2
    require(0 <= index < MAP_SIZE * MAP_SIZE and (offset - start) % 2 == 0,
            "Permission offset is outside the map's permission section")
    ox, oz = context["origin"]
    return ox + index % MAP_SIZE, oz + index // MAP_SIZE


def permission_rows(raw, context, overrides=None):
    """32 rows of ``type:collision`` hex pairs; compact enough for JSON and Qt."""
    start = context["sections"]["permissions_offset"]
    overrides = overrides or {}
    rows = []
    for lz in range(MAP_SIZE):
        row = []
        for lx in range(MAP_SIZE):
            offset = start + 2 * (lz * MAP_SIZE + lx)
            row.append(overrides.get(offset, raw[offset:offset + 2]).hex())
        rows.append(row)
    return rows


def is_blocked(pair):
    kind, flags = pair[0], pair[1]
    # MapFile.cs: 0x80 is the blocking flag; water/void types block ordinary walking.
    return bool(flags & 128) or kind in (16, 21)


def local_position(context, raw_axis):
    """Placement 16.16 record value -> global tile coordinate."""
    return raw_axis / 65536 + MAP_SIZE // 2


def record_value(value):
    """Global tile coordinate -> exact 16.16 record value, or refuse."""
    scaled = value * 65536
    require(float(scaled).is_integer() and -(2 ** 31) <= scaled < 2 ** 31,
            "Coordinates must be exact 1/65536 tile steps inside the record range", "UNQUALIFIED_TARGET")
    return int(scaled)
