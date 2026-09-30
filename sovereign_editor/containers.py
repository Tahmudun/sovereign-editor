"""Preserving NARC member replacement and NTR ROM file relocation.

Unchanged members keep their exact offsets and bytes. Resized members append to
the existing image; the original bytes remain as unreferenced data. A grown ROM
file appends on a 512-byte boundary, keeping its file ID. Never normalize a ROM.
"""
import struct

import ndspy.fnt
from ndspy._common import crc16

from .formats import digest, file_span, member_span, require, span


def append_members(raw, payloads):
    """Append unnamed members, preserving every existing member and block payload."""
    if not payloads:
        return bytes(raw)
    count = struct.unpack_from('<H', raw, 24)[0]
    require(count + len(payloads) < 65535, 'Archive member capacity exceeded', 'RESOURCE_CAPACITY')
    old = [member_span(raw, i)[1] for i in range(count)]
    fat_end = 16 + struct.unpack_from('<I', raw, 20)[0]
    fnt_end = fat_end + struct.unpack_from('<I', raw, fat_end + 4)[0]
    # These game archives use the eight-byte empty filename table. Named archives
    # would require a separate filename allocation operation.
    require(raw[fat_end + 8:fnt_end] == b'\x04\x00\x00\x00\x00\x00\x01\x00',
            'Only unnamed resource archives can grow', 'UNSUPPORTED_ARCHIVE')
    image = bytearray(raw[fnt_end + 8:])
    entries = bytearray()
    for data in payloads:
        image.extend(b'\xff' * (-len(image) % 4))
        start = len(image)
        image.extend(data)
        entries.extend(struct.pack('<II', start, len(image)))
    image.extend(b'\xff' * (-len(image) % 4))
    insertion = 28 + count * 8
    result = bytearray(raw[:insertion] + entries + raw[insertion:fat_end] + raw[fat_end:fnt_end])
    result.extend(b'GMIF' + struct.pack('<I', len(image) + 8) + image)
    struct.pack_into('<H', result, 24, count + len(payloads))
    struct.pack_into('<I', result, 20, fat_end - 16 + len(entries))
    struct.pack_into('<I', result, 8, len(result))
    for i, data in enumerate(old + list(payloads)):
        require(member_span(result, i)[1] == data, 'Appended archive readback differs')
    return bytes(result)


def replace_members(raw, replacements):
    result = bytearray(raw)
    # Validate the complete allocation table, including untouched entries.
    count = struct.unpack_from("<H", span(raw, 24, 2))[0]
    members = [member_span(raw, i) for i in range(count)]
    intervals = sorted((offset, offset + len(data)) for offset, data in members if data)
    require(all(end <= following for (_, end), (following, _) in zip(intervals, intervals[1:])),
            "Overlapping NARC members are unsupported", "UNSUPPORTED_ARCHIVE")
    fat_size = struct.unpack_from("<I", raw, 20)[0]
    fnt = 16 + fat_size
    image = fnt + struct.unpack_from("<I", raw, fnt + 4)[0]
    changes = []
    for index, payload in sorted(replacements.items()):
        require(type(index) is int and 0 <= index < count, "Invalid replacement member")
        offset, before = members[index]
        if before == payload:
            continue
        if len(payload) == len(before):
            result[offset:offset + len(before)] = payload
            target = offset
        else:
            result.extend(b"\xff" * (-len(result) % 4))
            target = len(result)
            result.extend(payload)
            struct.pack_into("<II", result, 28 + index * 8,
                             target - image - 8, target - image - 8 + len(payload))
        changes.append({"member": index, "before_bytes": len(before), "after_bytes": len(payload),
                        "before_sha256": digest(before), "after_sha256": digest(payload),
                        "relocated": offset != target})
    if len(result) != len(raw):
        result.extend(b"\xff" * (-len(result) % 4))
        struct.pack_into("<I", result, 8, len(result))
        struct.pack_into("<I", result, image + 4, len(result) - image)
    for i, (_, before) in enumerate(members):
        require(member_span(result, i)[1] == replacements.get(i, before),
                f"Archive member {i} readback differs")
    return bytes(result), changes


def replace_file(blob, name, payload):
    fnt, fnt_len = struct.unpack_from('<II', blob, 0x40)
    file_id = ndspy.fnt.load(span(blob, fnt, fnt_len)).idOf(name)
    require(file_id is not None, 'Missing ROM file')
    return replace_file_id(blob, file_id, payload)


BANNER_BYTES = {1: 0x840, 2: 0x940, 3: 0xA40, 0x103: 0x23C0}


def _regions(blob):
    """(start, end, file id or None) of everything the header and FAT declare."""
    regions = []
    for offset in (0x20, 0x30):
        at, _, _, size = struct.unpack_from("<4I", blob, offset)
        regions.append((at, at + size, None))
    fnt, fnt_len, fat, fat_len, ov9, ov9_len, ov7, ov7_len = struct.unpack_from("<8I", blob, 0x40)
    regions += [(fnt, fnt + fnt_len, None), (fat, fat + fat_len, None), (ov9, ov9 + ov9_len, None),
                (ov7, ov7 + ov7_len, None)]
    banner = struct.unpack_from("<I", blob, 0x68)[0]
    if banner and banner + 2 <= len(blob):
        regions.append((banner, banner + BANNER_BYTES.get(struct.unpack_from("<H", blob, banner)[0], 0x23C0), None))
    for i in range(fat_len // 8):
        a, b = struct.unpack_from("<II", blob, fat + 8 * i)
        if b > a:
            regions.append((a, b, i))
    return regions


def _append(result, payload, capacity):
    target = (len(result) + 511) & ~511
    end = target + len(payload)
    require(end <= capacity, "Structural export exceeds this ROM's declared capacity", "ROM_CAPACITY")
    result.extend(b"\xff" * (target - len(result)))
    result.extend(payload)
    return target, end


def _grow_in_place(blob, file_id, start, payload):
    """Grow a file where it lies when the files it would overlap are smaller than it: those
    files move (unchanged) to the end first. None when relocating the file itself is better."""
    from .character_runtime import file_by_id
    need = start + len(payload)
    try:
        regions = _regions(blob)
    except struct.error:
        return None                       # a header/FAT that does not describe this file: refuse by capacity
    blockers = [r for r in regions if r[2] != file_id and r[0] < need and r[1] > start]
    if any(r[2] is None for r in blockers) or sum(r[1] - r[0] for r in blockers) >= len(payload):
        return None
    fat = struct.unpack_from("<I", blob, 0x48)[0]
    capacity = 128 * 1024 << blob[0x14]
    result = bytearray(blob)
    moved = []
    for a, b, i in sorted(blockers):
        target, end = _append(result, bytes(blob[a:b]), capacity)
        struct.pack_into("<II", result, fat + 8 * i, target, end)
        moved.append({"file_id": i, "old_start": a, "start": target, "bytes": b - a})
    require(need <= capacity, "Structural export exceeds this ROM's declared capacity", "ROM_CAPACITY")
    if need > len(result):
        result.extend(b"\xff" * (need - len(result)))
    result[start:need] = payload
    struct.pack_into("<II", result, fat + 8 * file_id, start, need)
    struct.pack_into("<I", result, 0x80, max(struct.unpack_from("<I", blob, 0x80)[0], len(result)))
    struct.pack_into("<H", result, 0x15E, crc16(result[:0x15E]))
    # Original-prefix preservation: only the grown extent, the moved files' and this file's
    # allocation entries and the two header fields may differ.
    restored = bytearray(result[:len(blob)])
    fields = [(start, len(payload)), (fat + 8 * file_id, 8), (0x80, 4), (0x15E, 2)]
    fields += [(fat + 8 * m["file_id"], 8) for m in moved]
    for offset, size in fields:
        size = max(0, min(size, len(blob) - offset))
        restored[offset:offset + size] = blob[offset:offset + size]
    require(restored == blob, "Unexpected original ROM bytes changed during in-place growth")
    require(file_by_id(result, file_id)[1] == payload, "Grown file readback differs")
    for m in moved:
        require(file_by_id(result, m["file_id"])[1] == blob[m["old_start"]:m["old_start"] + m["bytes"]],
                "Moved file readback differs")
    return bytes(result), {"relocated": False, "grown_in_place": True, "changed": True, "file_id": file_id,
                           "start": start, "old_bytes": len(file_by_id(blob, file_id)[1]), "bytes": len(payload),
                           "moved_files": moved, "appended_bytes": len(result) - len(blob),
                           "original_prefix_preserved_except_metadata": True}


def replace_file_id(blob, file_id, payload):
    """Same preservation rules for unnamed overlay files."""
    from .character_runtime import file_by_id
    start, before = file_by_id(blob, file_id)
    if payload == before:
        return bytes(blob), {"relocated": False, "changed": False}
    result = bytearray(blob)
    if len(payload) == len(before):
        result[start:start + len(before)] = payload
        return bytes(result), {"relocated": False, "changed": True, "start": start,
                               "bytes": len(payload)}
    require(blob[0x12] == 0, "Structural exports support NTR ROMs only", "UNSUPPORTED_ROM")
    if len(payload) > len(before) and ((len(blob) + 511) & ~511) + len(payload) > 128 * 1024 << blob[0x14]:
        # Relocation (the historical rule, byte-identical for every earlier export) no longer fits
        # the declared capacity: grow in place when the files in the way are smaller.
        grown = _grow_in_place(blob, file_id, start, payload)
        if grown is not None:
            return grown
    fnt, fnt_len, fat, fat_len = struct.unpack_from("<4I", blob, 0x40)
    require(8 * (file_id + 1) <= fat_len, "Missing file allocation")
    target = (len(blob) + 511) & ~511
    end = target + len(payload)
    capacity = 128 * 1024 << blob[0x14]
    require(end <= capacity, "Structural export exceeds this ROM's declared capacity",
            "ROM_CAPACITY")
    result.extend(b"\xff" * (target - len(result)))
    result.extend(payload)
    allocation = fat + 8 * file_id
    struct.pack_into("<II", result, allocation, target, end)
    struct.pack_into("<I", result, 0x80, end)
    struct.pack_into("<H", result, 0x15E, crc16(result[:0x15E]))
    # Exact original-prefix preservation, except the three declared fields.
    restored = bytearray(result[:len(blob)])
    fields = [(allocation, 8), (0x80, 4), (0x15E, 2)]
    for offset, size in fields:
        restored[offset:offset + size] = blob[offset:offset + size]
    require(restored == blob, "Unexpected original ROM bytes changed during relocation")
    require(file_by_id(result, file_id)[1] == payload, "Relocated file readback differs")
    return bytes(result), {"relocated": True, "changed": True, "file_id": file_id,
                           "old_start": start, "old_bytes": len(before), "start": target,
                           "bytes": len(payload), "appended_bytes": len(result) - len(blob),
                           "metadata_fields": [{"offset": o, "bytes": n} for o, n in fields],
                           "original_prefix_preserved_except_metadata": True}
