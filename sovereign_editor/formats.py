"""Read only the qualified HGSS records; preserve original containers on write."""
import hashlib
import json
import struct
from pathlib import Path

import ndspy.codeCompression
import ndspy.fnt

ASSETS = Path(__file__).parent / "assets" / "cherrygrove"


class EditorError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def require(condition, message, code="INVALID_DATA"):
    if not condition:
        raise EditorError(code, message)


def digest(blob):
    return hashlib.sha256(blob).hexdigest()


_IMMUTABLE_DIGEST = [None, None]     # (bytes object, sha256): the last immutable ROM image hashed


def immutable_digest(blob):
    """sha256 of an immutable ROM image, hashed once per object (whole-state validation
    checks the pinned baseline several times per plan). Mutable buffers are always rehashed."""
    if type(blob) is not bytes:
        return digest(blob)
    if _IMMUTABLE_DIGEST[0] is not blob:
        _IMMUTABLE_DIGEST[:] = [blob, digest(blob)]
    return _IMMUTABLE_DIGEST[1]


def baseline_digest(project):
    """Reuse Project's verified immutable image hash; simple format probes also work."""
    cached = getattr(project, 'baseline_sha256', None)
    return digest(project.blob) if cached is None else cached


def span(blob, offset, size):
    require(offset >= 0 and size >= 0 and offset + size <= len(blob), "Record exceeds container bounds")
    return blob[offset:offset + size]


_FNT = {}


def _filenames(table):
    """Parsed filename table, keyed by its own bytes (content-addressed, so any
    ROM with a different table misses). Parsing dominated repeated lookups."""
    folder = _FNT.get(table)
    if folder is None:
        folder = ndspy.fnt.load(table)
        if len(_FNT) >= 8:
            _FNT.pop(next(iter(_FNT)))
        _FNT[table] = folder
    return folder


# Per immutable ROM image (compared by identity, like _IMMUTABLE_DIGEST): file spans
# by name and parsed NARC member tables by archive. Lookups then slice only the
# requested bytes instead of copying the filename table and a whole archive each
# call (PROD-PERF-001). Mutable buffers are never cached.
_ROM_TABLES = []
_ROM_TABLE_LIMIT = 2


def _rom_tables(blob):
    if type(blob) is not bytes:
        return None
    for entry in _ROM_TABLES:
        if entry[0] is blob:
            return entry
    entry = (blob, {}, {})
    _ROM_TABLES.insert(0, entry)
    del _ROM_TABLES[_ROM_TABLE_LIMIT:]
    return entry


def _file_bounds(blob, name):
    tables = _rom_tables(blob)
    if tables is not None and name in tables[1]:
        return tables[1][name]
    require(len(blob) >= 0x200, "ROM is too short")
    fnt, fnt_len, fat, fat_len = struct.unpack_from("<4I", blob, 0x40)
    filenames = _filenames(bytes(span(blob, fnt, fnt_len)))
    index = filenames.idOf(name)
    require(index is not None and 8 * (index + 1) <= fat_len, f"ROM resource absent: {name}")
    start, end = struct.unpack_from("<II", span(blob, fat, fat_len), 8 * index)
    require(start <= end, "Invalid file allocation")
    require(end <= len(blob), "Record exceeds container bounds")
    if tables is not None:
        tables[1][name] = (start, end)
    return start, end


def file_span(blob, name):
    start, end = _file_bounds(blob, name)
    return start, span(blob, start, end - start)


def _narc_table(blob, archive):
    """(archive base, member data base, [(start, end)], count), validated as member_span does."""
    tables = _rom_tables(blob)
    if tables is not None and archive in tables[2]:
        return tables[2][archive]
    base, raw = file_span(blob, archive)
    require(raw[:4] == b"NARC" and len(raw) >= 28, "Invalid NARC")
    declared, header_size, blocks = struct.unpack_from("<IHH", raw, 8)
    require(declared == len(raw) and header_size == 16 and blocks == 3, "Unsupported NARC layout")
    require(raw[16:20] == b"BTAF", "NARC allocation table absent")
    fat_size, count = struct.unpack_from("<IH", raw, 20)
    require(fat_size >= 12 + 8 * count, "Invalid member index/table")
    fnt = 16 + fat_size
    require(span(raw, fnt, 4) == b"BTNF", "NARC filename block absent")
    fnt_size = struct.unpack_from("<I", span(raw, fnt + 4, 4))[0]
    image = fnt + fnt_size
    require(span(raw, image, 4) == b"GMIF", "NARC image block absent")
    image_size = struct.unpack_from("<I", span(raw, image + 4, 4))[0]
    require(image + image_size == len(raw), "Unexpected NARC trailing data")
    members = [struct.unpack_from("<II", raw, 28 + i * 8) for i in range(count)]
    table = (base, image + 8, image_size - 8, members, count)
    if tables is not None:
        tables[2][archive] = table
    return table


def member_span(raw, index):
    require(raw[:4] == b"NARC" and len(raw) >= 28, "Invalid NARC")
    declared, header_size, blocks = struct.unpack_from("<IHH", raw, 8)
    require(declared == len(raw) and header_size == 16 and blocks == 3, "Unsupported NARC layout")
    require(raw[16:20] == b"BTAF", "NARC allocation table absent")
    fat_size, count = struct.unpack_from("<IH", raw, 20)
    require(0 <= index < count and fat_size >= 12 + 8 * count, "Invalid member index/table")
    fnt = 16 + fat_size
    require(span(raw, fnt, 4) == b"BTNF", "NARC filename block absent")
    fnt_size = struct.unpack_from("<I", span(raw, fnt + 4, 4))[0]
    image = fnt + fnt_size
    require(span(raw, image, 4) == b"GMIF", "NARC image block absent")
    image_size = struct.unpack_from("<I", span(raw, image + 4, 4))[0]
    require(image + image_size == len(raw), "Unexpected NARC trailing data")
    start, end = struct.unpack_from("<II", span(raw, 28 + index * 8, 8))
    require(start <= end and end <= image_size - 8, "Member outside NARC image")
    return image + 8 + start, span(raw, image + 8 + start, end - start)


def resource(blob, archive, index):
    base, data, image_size, members, count = _narc_table(blob, archive)
    require(0 <= index < count, "Invalid member index/table")
    start, end = members[index]
    require(start <= end and end <= image_size, "Member outside NARC image")
    return base + data + start, blob[base + data + start:base + data + end]


def member_count(blob, archive):
    return _narc_table(blob, archive)[4]


def arm9_code(blob):
    """Decompressed ARM9, cached per ROM so a project reads it once."""
    # The same immutable ROM object again (the common case) skips re-hashing the compressed ARM9.
    if type(blob) is bytes and _ARM9_LAST[0] is blob:
        return _ARM9_LAST[1]
    require(len(blob) >= 0x40, "ROM is too short")
    start, _, _, size = struct.unpack_from("<4I", blob, 0x20)
    compressed = span(blob, start, size)
    key = digest(compressed)
    if key not in _ARM9_CACHE:
        _ARM9_CACHE.clear()
        _ARM9_CACHE[key] = ndspy.codeCompression.decompress(compressed)
    if type(blob) is bytes:
        _ARM9_LAST[:] = [blob, _ARM9_CACHE[key]]
    return _ARM9_CACHE[key]


_ARM9_CACHE = {}
_ARM9_LAST = [None, None]


def map_sections(raw):
    """MapFile.cs section table: lengths, the HGSS BGS block and every offset.

    Generalised from the Cherrygrove-only reader; no section is reconstructed, so
    BGS, model, terrain/BDHC and every unknown byte stay exactly where they are.
    """
    require(len(raw) >= 20, "Map header absent")
    permissions, buildings, model, terrain = struct.unpack_from("<4I", raw)
    signature, extra = struct.unpack_from("<HH", raw, 16)
    require(signature == 0x1234, "Unsupported HGSS background-sound header")
    require(permissions == 2048 and buildings % 48 == 0, "Unsupported HGSS map sections")
    require(20 + extra + permissions + buildings + model + terrain == len(raw), "Map sections disagree")
    start = 20 + extra
    return {"bgs_offset": 16, "bgs_bytes": 4 + extra,
            "permissions_offset": start, "permissions_bytes": permissions,
            "buildings_offset": start + permissions, "buildings_bytes": buildings,
            "building_count": buildings // 48, "building_record_bytes": 48,
            "model_offset": start + permissions + buildings, "model_bytes": model,
            "terrain_offset": start + permissions + buildings + model, "terrain_bytes": terrain,
            "total_bytes": len(raw)}


def map_data(raw):
    sections = map_sections(raw)
    permissions, buildings = sections["permissions_bytes"], sections["buildings_bytes"]
    start = sections["permissions_offset"]
    collision = raw[start:start + permissions]
    props = []
    start += permissions
    for slot in range(buildings // 48):
        p = start + slot * 48
        raw_xyz = list(struct.unpack_from("<3i", raw, p + 4))
        props.append({"slot": slot, "model_id": struct.unpack_from("<I", raw, p)[0],
                      # Both forms of the same words: xyz_raw drives writes, xyz displays.
                      "xyz_raw": raw_xyz, "xyz": [v / 65536 for v in raw_xyz],
                      "record_offset": p, "record_sha256": digest(raw[p:p + 48]),
                      "rotation_raw": list(struct.unpack_from("<3i", raw, p + 16)),
                      "scale_raw": list(struct.unpack_from("<3i", raw, p + 28)),
                      "unknown_hex": raw[p + 40:p + 48].hex()})
    return collision, props, raw[sections["model_offset"]:sections["model_offset"] + sections["model_bytes"]]


def events(raw):
    cursor, sections = 0, {}
    for name, size in [("backgrounds", 20), ("npcs", 32), ("warps", 12), ("triggers", 16)]:
        count = struct.unpack_from("<I", span(raw, cursor, 4))[0]
        cursor += 4
        require(count < 10000, "Invalid event count")
        records = span(raw, cursor, count * size)
        sections[name] = (cursor, [records[i * size:(i + 1) * size] for i in range(count)])
        cursor += count * size
    require(cursor == len(raw), "Unsupported trailing event data")
    npc_start, npc_raw = sections["npcs"]
    npcs = []
    for slot, item in enumerate(npc_raw):
        v = struct.unpack("<12H2Hi", item)
        npcs.append({"id": v[0], "sprite": v[1], "movement": v[2], "flag": v[4],
                     "script": v[5], "facing": v[6], "range_x": v[10], "range_z": v[11],
                     "x": v[12], "z": v[13], "y": v[14], "offset": npc_start + slot * 32 + 24})
    require(len({v["id"] for v in npcs}) == len(npcs), "Ambiguous NPC IDs")
    warps = []
    for i, item in enumerate(sections["warps"][1]):
        v = struct.unpack("<6H", item)
        warps.append({"id": i, "x": v[0], "z": v[1], "destination": v[2], "destination_warp": v[3]})
    triggers = []
    for i, item in enumerate(sections["triggers"][1]):
        v = struct.unpack("<8H", item)
        triggers.append({"id": i, "script": v[0], "x": v[1], "z": v[2], "width": v[3], "height": v[4]})
    backgrounds = []
    for i, item in enumerate(sections["backgrounds"][1]):
        script, kind, x, z, y, direction = struct.unpack("<HH4i", item)
        backgrounds.append({"id": i, "script": script, "kind": kind, "x": x, "z": z,
                            "y": y, "direction": direction})
    return {"npcs": npcs, "warps": warps, "triggers": triggers, "backgrounds": backgrounds}


def flat_height_plates(raw, *, point=None):
    """Read the rectangular, horizontal BDHC subset; never rewrite terrain."""
    permissions, buildings, model, terrain = struct.unpack_from("<4I", raw)
    extra = struct.unpack_from("<H", raw, 18)[0]
    data = span(raw, 20 + extra + permissions + buildings + model, terrain)
    require(span(data, 0, 4) == b"BDHC", "Terrain height header absent")
    counts = struct.unpack_from("<6H", span(data, 4, 12))
    cursor, arrays = 16, []
    for count, fmt in zip(counts, ("<2i", "<3i", "<i", "<4H", "<i2H", "<H")):
        size = struct.calcsize(fmt)
        section = span(data, cursor, count * size)
        arrays.append(list(struct.iter_unpack(fmt, section)))
        cursor += count * size
    require(cursor == len(data), "Unexpected BDHC trailing data")
    points, normals, constants, plates, strips, indices = arrays
    for _, count, start in strips:
        require(start + count <= len(indices), "Invalid BDHC strip")
    require(all(i[0] < len(plates) for i in indices), "Invalid BDHC plate index")
    result = []
    for index, (first, second, normal, constant) in enumerate(plates):
        require(max(first, second) < len(points) and normal < len(normals) and constant < len(constants),
                "Invalid BDHC reference")
        x0, z0 = points[first]
        x1, z1 = points[second]
        require(x0 < x1 and z0 < z1, "Unsupported BDHC rectangle")
        # Scene paths may use a flat part of a map containing unrelated slopes.
        # A slope covering the requested point remains unsupported; the default
        # whole-map qualification used by existing authoring stays unchanged.
        if point is not None and not (x0 <= point[0]*65536 < x1 and z0 <= point[1]*65536 < z1):
            continue
        require(normals[normal] == (0, 4096, 0), "Only horizontal BDHC planes qualified")
        result.append({"index": index, "bounds": [v / 65536 for v in (x0, z0, x1, z1)],
                       "height": -constants[constant][0] / 65536})
    return result


def qualify(blob):
    profile = json.loads((ASSETS / "profile.json").read_text())
    require(blob[12:16].decode("ascii", errors="replace") == "IPKE", "This release supports the US HeartGold profile", "UNSUPPORTED_ROM")
    arm9 = arm9_code(blob)
    header = span(arm9, 0xF6BE0 + 67 * 24, 24)
    require(header.hex() == profile["header_hex"], "Cherrygrove header differs from the qualified profile", "UNSUPPORTED_ROM")
    for ref in profile["resources"]:
        _, payload = resource(blob, ref["archive"], ref["member"])
        require(digest(payload) == ref["sha256"],
                f"Unsupported modified resource: {ref['archive']} member {ref['member']}", "UNSUPPORTED_ROM")
    return profile
