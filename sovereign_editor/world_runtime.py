"""Resident map-header extension for the pinned runtime (WORLD-ALLOC-001).

pret/pokeheartgold e97c7fc src/map_header.c keeps ``sMapHeaders`` static; every
accessor calls the static ``MapNumberBoundsCheck`` and then reads
``sMapHeaders + field + id * 24`` with a 32-bit multiply. In the pinned ARM9 the
table (540 records at 0x020F6BE0) is followed by other data, so it cannot grow in
place, and the bounds check sends IDs >= 540 to GF_AssertFail and header 1.

Repair: the created records live in overlay 129, the ARM9 extension that hg-engine
loads once from Main() (armips/asm/syntheticoverlay.s) into its own reserved
0x023D8000..0x023E0000 region and never unloads. The bounds entry tail-jumps to
a hook that returns stock IDs unchanged and maps created ID 540+k to the index
whose stock address arithmetic lands on extension record k. Out-of-range IDs run
the original GF_AssertFail/header-1 instructions. The stock table, its accessors
and the internal-name table offsets stay byte-identical. Pure candidate bytes;
Project owns writes.
"""
import functools
import struct

from . import character_runtime as cr
from .formats import digest, immutable_digest, require, span

ARM_BASE = 0x02000000
TABLE = 0x020F6BE0             # sMapHeaders, uncompressed ARM9
RECORD = 24
BASE_COUNT = 540               # MAP_ID_MAX in the pinned build
BOUNDS = 0x0203B268            # MapNumberBoundsCheck
FALLBACK = 0x0203B272          # bl GF_AssertFail; movs r0,#1; pop {r3,pc}
ASSERT_FAIL = 0x0202551C
V1_MAX_HEADERS = 32            # historical layout v1 bound (full 24-byte records on the tail)
# Editor bound (layout v2). The hook loads the count from a literal (no 8-bit field), so the
# binding limit is resident overlay-129 space: 14 bytes per created header (+12 with an identity
# override), refused by the allocator. Raised from 128 for editor v1 (the r64 parent already holds
# 128 created headers, 124 of them production-load rooms).
MAX_HEADERS = 152
COMPACT = struct.Struct('<6HBx')   # template, matrix, script, text, events, coords, wild
# Layout v3 (world integration): the spare entry byte is a 1-based index into a table of
# identity overrides; 0 keeps the template's fields (a v3 table with no index is v2 data).
COMPACT_V3 = struct.Struct('<6HBB')
# Override record: header bytes 12..15 (day/night music), 20..23 (region/weather/type/camera/
# follow/battle background/flags), 18..19 (map section, popup icon, mom call) and byte 1 (area data).
OVERRIDE = struct.Struct('<IIHBx')
MAX_OVERRIDES = 255
# Area texture animation (PROD-VIS-002): ARM9 0x02054E20 answers "is this map member excluded
# from the area's index-bound texture animation" from a 3-entry stock table (members 208, 210,
# 211). Created copies of those members get new IDs; a resident list extends the rule.
STATIC_CHECK = 0x02054E20
STATIC_TABLE = 0x020FC5FC
STATIC_BEFORE = bytes.fromhex('10b5041c')        # push {r4,lr}; adds r4,r0,#0 (then bl 0x02054E00)
STATIC_STOCK = (208, 210, 211)
MAX_STATIC = 256
# Pokégear/Town Map labels (WORLD-MAP-001): overlay 101 PokegearMap_GetLocationSpecByCoord
# (0x021EA6E8) searches 100 stock 16-byte specs; its not-found tail (movs r0,#0; add sp,#8 at
# 0x021EA74C, then pop {r3-r7,pc}) becomes a BL to a resident search of created specs, so a
# stock label always wins and created locations only fill grid cells no stock spec covers.
TOWN_OVERLAY = 101
TOWN_ADDRESS = 0x021E7740
TOWN_TAIL = 0x021EA74C
TOWN_TAIL_BEFORE = bytes.fromhex('002002b0')
TOWN_SPECS = 0x021F7372
TOWN_STOCK_COUNT = 100
SPEC = struct.Struct('<HBBHBB4x4B')             # mapId, x, y, w|h<<4|objX<<8|objY<<12, flavor, block, unused, tilemap
MAX_TOWN_SPECS = 32
# Header fields a created area changes; every other byte is its stock template's.
# The level script is always allocated right after the area's local scripts.
EXTENSION_LIMIT = 0x8000       # hg-engine src/linker.ld region for overlay 129
# push {r3,lr}; movs r1,#0x87; lsls r1,#2; cmp r0,r1; blo; bl GF_AssertFail; movs r0,#1; pop {r3,pc}
BOUNDS_BYTES = bytes.fromhex('08b5872189008842' '02d3' 'eaf753f9' '0120' '08bd')


def _bl_target(source, first, second):
    if first >> 11 != 0x1E or second >> 11 != 0x1F:
        return None
    offset = ((first & 0x7FF) << 12) | ((second & 0x7FF) << 1)
    if offset & (1 << 22):
        offset -= 1 << 23
    return source + 4 + offset


def consumers(arm):
    """Every direct call to the bounds check, with its getter entry point.

    Each call must be followed by the stock ``id * 24`` indexing sequence; a
    consumer that uses the returned ID any other way refuses the repair.
    """
    return [dict(c) for c in _consumers(bytes(arm))]


@functools.lru_cache(maxsize=4)
def _consumers(arm):
    """Content-keyed scan (an ARM9 image is scanned once per process)."""
    import numpy as np
    result = []
    words = np.frombuffer(arm[:len(arm) - len(arm) % 2], dtype='<u2').astype(np.int64)
    first, second = words[:-1], words[1:]
    offset = ((first & 0x7FF) << 12) | ((second & 0x7FF) << 1)
    offset = np.where(offset & (1 << 22), offset - (1 << 23), offset)
    source = ARM_BASE + 2 * np.arange(len(first), dtype=np.int64)
    hits = np.nonzero((first >> 11 == 0x1E) & (second >> 11 == 0x1F) & (source + 4 + offset == BOUNDS))[0]
    for offset in (2 * int(i) for i in hits):
        first, second = struct.unpack_from('<HH', arm, offset)
        require(_bl_target(ARM_BASE + offset, first, second) == BOUNDS, 'Bounds consumer scan disagrees')
        entry = next((offset - back for back in range(2, 10, 2)
                      if arm[offset - back + 1] == 0xB5), None)
        tail = arm[offset + 4:offset + 10]
        require(entry is not None and b'\x18\x21' in tail[:4] and b'\x41\x43' in tail,
                'An unqualified map-header bounds consumer exists', 'UNSUPPORTED_RUNTIME')
        result.append({'call': ARM_BASE + offset, 'entry': ARM_BASE + entry})
    return result


def hook(address, count, index):
    """Thumb; r0 is the header ID, exactly as at the bounds entry."""
    require(address % 4 == 0, 'Header hook must be word aligned')
    code = struct.pack('<14H', 0x4906, 0x4288, 0xD305, 0x1A42, 0x4905, 0x428A, 0xD202,
                       0x4805, 0x1880, 0x4770, 0xB508, 0x4904, 0x4708, 0x46C0)
    return code + struct.pack('<4I', BASE_COUNT, count, index, FALLBACK | 1)


def stub(address):
    """Bounds entry: tail-jump with lr intact (ldr r1,[pc]; bx r1; .word hook)."""
    return struct.pack('<2HI', 0x4900, 0x4708, address | 1)


def materialize(template, entry, overrides=()):
    """The 24-byte record the v2/v3 hook builds from a stock template and a compact entry."""
    _, matrix, script, text, events, coords, wild, index = COMPACT_V3.unpack(entry)
    raw = bytearray(template)
    raw[0] = wild
    struct.pack_into('<5H', raw, 2, coords, matrix, script, script + 1, text)
    struct.pack_into('<H', raw, 16, events)
    if index:
        require(index <= len(overrides), 'Header entry names a missing identity override', 'UNSUPPORTED_RUNTIME')
        music, packed, section, area = OVERRIDE.unpack(overrides[index - 1])
        struct.pack_into('<I', raw, 12, music)
        struct.pack_into('<I', raw, 20, packed)
        struct.pack_into('<H', raw, 18, section)
        raw[1] = area
    return bytes(raw)


def override(raw):
    """Identity override record carrying a created record's non-compact fields."""
    return OVERRIDE.pack(struct.unpack_from('<I', raw, 12)[0], struct.unpack_from('<I', raw, 20)[0],
                         struct.unpack_from('<H', raw, 18)[0], raw[1])


def compact(arm, raw, template, overrides=None):
    """Compact entry for a created record, refused unless it rebuilds exactly.

    ``overrides`` (a list, layout v3) receives any identity override the record needs;
    identical overrides are shared. Without it (v2) only template-identical identity fits.
    """
    require(type(template) is int and 0 <= template < BASE_COUNT, 'A created header needs a stock template', 'INVALID_INPUT')
    wild, _, coords, matrix, script, level, text = struct.unpack_from('<BBH4H', raw, 0)
    events = struct.unpack_from('<H', raw, 16)[0]
    entry = COMPACT.pack(template, matrix, script, text, events, coords, wild)
    stock = arm[TABLE - ARM_BASE + template * RECORD:TABLE - ARM_BASE + (template + 1) * RECORD]
    if materialize(stock, entry) == raw or overrides is None:
        require(materialize(stock, entry) == raw,
                'Created header differs from its template outside the area-owned fields', 'UNSUPPORTED_RUNTIME')
        return entry
    record = override(raw)
    if record not in overrides:
        require(len(overrides) < MAX_OVERRIDES, f'At most {MAX_OVERRIDES} distinct identity overrides are supported',
                'RESOURCE_CAPACITY')
        overrides.append(record)
    entry = COMPACT_V3.pack(template, matrix, script, text, events, coords, wild, overrides.index(record) + 1)
    require(materialize(stock, entry, overrides) == raw,
            'Created header differs from its template outside the supported identity fields', 'UNSUPPORTED_RUNTIME')
    return entry


def _v2_program(count, records, slot):
    copy = [h for k in range(6) for h in (0x6803 | k << 6, 0x600B | k << 6)]   # ldr r3,[r0,#4k]; str r3,[r1,#4k]
    fields = [0x8853, 0x808B,    # matrix  -> +4
              0x8893, 0x80CB,    # scripts -> +6
              0x3301, 0x810B,    # level script = scripts + 1 -> +8
              0x88D3, 0x814B,    # text -> +10
              0x8913, 0x820B,    # events -> +16
              0x8953, 0x804B,    # world-map coordinates -> +2
              0x7B13, 0x700B]    # wild table (u8) -> +0
    index = ((slot - TABLE) // RECORD) & 0xFFFFFFFF
    return [('ldr', 1, BASE_COUNT), 0x4288, ('b', 3, 'stock'),        # blo stock
            0x1A42, ('ldr', 1, count), 0x428A, ('b', 2, 'invalid'),     # k >= count
            0x210E, 0x434A, ('ldr', 1, records), 0x1852,                # r2 = &entry[k]
            0x8810, 0x2118, 0x4348, ('ldr', 1, TABLE), 0x1840,          # r0 = &stock[template]
            ('ldr', 1, slot), *copy, *fields, 'final',
            ('ldr', 0, index), 0x4770,
            'stock', 0x4770,
            'invalid', 0xB508, ('ldr', 1, FALLBACK | 1), 0x4708]


def hook_v2(count, records, slot):
    """Thumb, r0 = header ID as at the bounds entry. Stock IDs return unchanged;
    created ID 540+k copies its stock template into ``slot``, applies entry k and
    returns the index whose stock arithmetic lands on the slot; other IDs run the
    original GF_AssertFail/header-1 tail. Clobbers only r0-r3 (caller-saved)."""
    from .resident import thumb
    return thumb([op for op in _v2_program(count, records, slot) if op != 'final'], 0)


OVERRIDE_CODE = [0x7B53, 0x2B00, ('b', 0, 'done'),   # ldrb r3,[r2,#13]; cmp r3,#0; beq done
                 0x3B01, 0x200C, 0x4343,               # subs r3,#1; movs r0,#12; muls r3,r0
                 ('ldr', 0, 'overrides'), 0x18C2,      # r2 = &override[index - 1]
                 0x6813, 0x60CB,                       # music      -> +12
                 0x6853, 0x614B,                       # packed u32 -> +20
                 0x8913, 0x824B,                       # section/icon/mom -> +18
                 0x7A93, 0x704B,                       # area data  -> +1
                 'done']


def hook_v3(count, records, slot, overrides):
    """hook_v2 plus identity overrides: after materializing entry k, a nonzero spare byte
    selects a 12-byte override that replaces the template's identity fields. Same
    register use (r0-r3, caller-saved); stock and invalid IDs take the v2 paths."""
    from .resident import thumb
    program = _v2_program(count, records, slot)
    at = program.index('final')
    body = [('ldr', 0, overrides) if op == ('ldr', 0, 'overrides') else op for op in OVERRIDE_CODE]
    return thumb(program[:at] + body + program[at + 1:], 0)


def bindings(blob, plan, records, layout=None):
    """Add created header records (IDs 540..) to a runtime plan; no IO.

    ``records`` are 24-byte records, or (record, stock template) pairs. With a
    resident.Layout (v2) records are stored compactly and materialized on lookup.
    """
    if not records:
        return plan
    entries = [r if isinstance(r, tuple) else (r, None) for r in records]
    require(isinstance(records, list) and all(isinstance(r, bytes) and len(r) == RECORD for r, _ in entries),
            'Created map headers need 24-byte records', 'INVALID_INPUT')
    if layout is not None:
        return _bindings_v2(blob, plan, entries, layout)
    records = [r for r, _ in entries]
    require(len(records) <= V1_MAX_HEADERS, f'At most {V1_MAX_HEADERS} created map headers are supported', 'RESOURCE_CAPACITY')
    require(immutable_digest(blob) == cr.BASELINE, 'Created map headers are qualified only for the pinned baseline', 'UNSUPPORTED_RUNTIME')
    arm_start = struct.unpack_from('<I', blob, 0x20)[0]
    before = span(blob, arm_start + BOUNDS - ARM_BASE, len(BOUNDS_BYTES))
    require(before == BOUNDS_BYTES, 'Map-header bounds check differs from the qualified build', 'BEFORE_VALUE_MISMATCH')
    arm = span(blob, arm_start, struct.unpack_from('<I', blob, 0x2C)[0])
    require(len(consumers(arm)) == 27, 'Map-header accessor set differs from the qualified build', 'UNSUPPORTED_RUNTIME')
    stub_offset = arm_start + BOUNDS - ARM_BASE
    require(not any(p['rom_offset'] < stub_offset + 8 and stub_offset < p['rom_offset'] + len(p['before']) // 2
                    for p in plan['patches']), 'Another runtime edit owns the bounds check', 'RESOURCE_CONFLICT')
    resident = cr.overlay(blob, 129)
    data = bytearray(plan['files'].get(resident['file_id'], resident['data']))
    data.extend(b'\0' * (-len(data) % 4))
    hook_address = resident['address'] + len(data)
    table = hook_address + len(hook(hook_address, 0, 0))
    table += (-(table - TABLE)) % RECORD
    index = (table - TABLE) // RECORD
    data.extend(hook(hook_address, len(records), index))
    data.extend(b'\0' * (table - resident['address'] - len(data)))
    data.extend(b''.join(records))
    require(len(data) <= EXTENSION_LIMIT,
            'Created map headers exceed the ARM9 extension reservation', 'RESOURCE_CAPACITY')
    result = {**plan, 'files': dict(plan['files']), 'patches': [dict(p) for p in plan['patches']]}
    result['patches'].append({'rom_offset': stub_offset, 'before': before[:8].hex(),
                              'after': stub(hook_address).hex(), 'kind': 'world.header-bounds'})
    result['files'][resident['file_id']] = bytes(data)
    size = resident['table_offset'] + 8
    existing = next((p for p in result['patches'] if p['rom_offset'] == size), None)
    if existing:
        existing['after'] = struct.pack('<I', len(data)).hex()
    else:
        result['patches'].append({'rom_offset': size, 'before': span(blob, size, 4).hex(),
                                  'after': struct.pack('<I', len(data)).hex(), 'kind': 'world.extension-size'})
    result['world_headers'] = {'first_id': BASE_COUNT, 'count': len(records), 'hook': hook_address,
                               'table': table, 'index_base': index, 'bounds': BOUNDS,
                               'extension_bytes': len(data), 'extension_limit': EXTENSION_LIMIT}
    return result


def _bindings_v2(blob, plan, entries, layout):
    require(len(entries) <= MAX_HEADERS, f'At most {MAX_HEADERS} created map headers are supported', 'RESOURCE_CAPACITY')
    require(immutable_digest(blob) == cr.BASELINE, 'Created map headers are qualified only for the pinned baseline', 'UNSUPPORTED_RUNTIME')
    arm_start = struct.unpack_from('<I', blob, 0x20)[0]
    before = span(blob, arm_start + BOUNDS - ARM_BASE, len(BOUNDS_BYTES))
    require(before == BOUNDS_BYTES, 'Map-header bounds check differs from the qualified build', 'BEFORE_VALUE_MISMATCH')
    arm = span(blob, arm_start, struct.unpack_from('<I', blob, 0x2C)[0])
    require(len(consumers(arm)) == 27, 'Map-header accessor set differs from the qualified build', 'UNSUPPORTED_RUNTIME')
    stub_offset = arm_start + BOUNDS - ARM_BASE
    require(not any(p['rom_offset'] < stub_offset + 8 and stub_offset < p['rom_offset'] + len(p['before']) // 2
                    for p in plan['patches']), 'Another runtime edit owns the bounds check', 'RESOURCE_CONFLICT')
    overrides = []
    table = b''.join(compact(arm, raw, template, overrides) for raw, template in entries)
    # v3 only when a created record carries its own identity; otherwise the v2 bytes stay exact.
    version = 3 if overrides else 2
    hook_size = len(hook_v3(0, 0, TABLE, 0) if overrides else hook_v2(0, 0, TABLE))
    hook = layout.place('world.header-hook', hook_size, 4, 'code', note='bounds-check entry tail jump (bx), not BL')
    slot = layout.place('world.header-slot', RECORD, 4, 'data', modulo=(RECORD, TABLE),
                        note='single materialized record; every getter reads one field immediately')
    # Historical placement (every earlier export stays byte-identical); items that no longer fit
    # overlay 129 overflow into the resident boot data region (resident.Layout.place, PROD-04).
    records = layout.place('world.header-records', len(table), 2, 'data', note=f'{len(entries)} x {COMPACT.size}-byte entries')
    identity = None
    if overrides:
        identity = layout.place('world.header-overrides', OVERRIDE.size * len(overrides), 4, 'data',
                                note=f'{len(overrides)} x {OVERRIDE.size}-byte identity overrides (music, packed flags, '
                                     'map section/popup, area data)')
        layout.write(identity, b''.join(overrides))
        layout.write(hook, hook_v3(len(entries), records, slot, identity))
    else:
        layout.write(hook, hook_v2(len(entries), records, slot))
    layout.write(records, table)
    result = {**plan, 'files': dict(plan['files']), 'patches': [dict(p) for p in plan['patches']]}
    result['patches'].append({'rom_offset': stub_offset, 'before': before[:8].hex(),
                              'after': stub(hook).hex(), 'kind': 'world.header-bounds'})
    result['world_headers'] = {'first_id': BASE_COUNT, 'count': len(entries), 'hook': hook, 'slot': slot,
                               'records': records, 'entry_bytes': COMPACT.size, 'bounds': BOUNDS, 'version': version,
                               'overrides': len(overrides), 'override_table': identity,
                               'override_bytes': OVERRIDE.size * len(overrides)}
    return result


def static_hook(members, count):
    """Thumb for ARM9 0x02054E20: r0 = map member. True for the three stock members of the
    stock table at STATIC_TABLE (read at run time, as the stock code does) or for any of the
    ``count`` created members listed at ``members``. Clobbers r0-r3 only."""
    from .resident import thumb
    return thumb([('ldr', 1, STATIC_TABLE), 0x2200,
                  'stock', 0x880B, 0x4298, ('b', 0, 'yes'), 0x3102, 0x3201, 0x2A03, ('b', 11, 'stock'),
                  ('ldr', 1, members), ('ldr', 2, count),
                  'created', 0x2A00, ('b', 0, 'no'), 0x880B, 0x4298, ('b', 0, 'yes'), 0x3102, 0x3A01,
                  ('b', None, 'created'),
                  'no', 0x2000, 0x4770,
                  'yes', 0x2001, 0x4770], 0)


def static_bindings(blob, plan, members, layout):
    """Exclude created map members from the area texture animation, like stock 208/210/211."""
    if not members:
        return plan
    members = sorted(set(members))
    require(len(members) <= MAX_STATIC and all(type(m) is int and 0 <= m < 0xFFFF for m in members),
            f'At most {MAX_STATIC} static map members are supported', 'RESOURCE_CAPACITY')
    arm_start = struct.unpack_from('<I', blob, 0x20)[0]
    at = arm_start + STATIC_CHECK - ARM_BASE
    before = span(blob, at, 8)
    require(before[:4] == STATIC_BEFORE and _bl_target(STATIC_CHECK + 4, *struct.unpack_from('<HH', before, 4)) == 0x02054E00,
            'Area animation exclusion check differs from the qualified build', 'BEFORE_VALUE_MISMATCH')
    stock = span(blob, arm_start + STATIC_TABLE - ARM_BASE, 6)
    require(struct.unpack('<3H', stock) == STATIC_STOCK, 'Stock static-member table differs', 'BEFORE_VALUE_MISMATCH')
    require(not any(p['rom_offset'] < at + 8 and at < p['rom_offset'] + len(p['before']) // 2 for p in plan['patches']),
            'Another runtime edit owns the animation exclusion check', 'RESOURCE_CONFLICT')
    code = layout.place('world.static-hook', len(static_hook(0, 0)), 4, 'code', note='0x02054E20 entry tail jump (bx)')
    table = layout.place('world.static-members', 2 * len(members), 2, 'data',
                         note=f'{len(members)} created map members without area texture animation')
    layout.write(code, static_hook(table, len(members)))
    layout.write(table, struct.pack(f'<{len(members)}H', *members))
    result = {**plan, 'files': dict(plan['files']), 'patches': [dict(p) for p in plan['patches']]}
    result['patches'].append({'rom_offset': at, 'before': before.hex(), 'after': stub(code).hex(),
                              'kind': 'world.static-members'})
    result['world_static'] = {'hook': code, 'members': members, 'table': table, 'check': STATIC_CHECK}
    return result


def town_hook(specs, count):
    """Thumb reached by BL from the not-found tail of PokegearMap_GetLocationSpecByCoord
    (r3 = x, r2 = y). Searches ``count`` created 16-byte specs with the stock tests
    (unsigned x/y lower bounds, signed exclusive upper bounds from the 4-bit width/height),
    returns the spec or NULL in r0, performs the stock ``add sp,#8`` and returns to the stock
    ``pop {r3-r7,pc}``. r4-r7 are restored by that pop."""
    from .resident import thumb
    return thumb([('ldr', 0, specs), ('ldr', 1, count),
                  'loop', 0x2900, ('b', 0, 'none'),
                  0x7884, 0x42A3, ('b', 3, 'next'),
                  0x78C5, 0x42AA, ('b', 3, 'next'),
                  0x8886, 0x0737, 0x0F3F, 0x19E4, 0x42A3, ('b', 10, 'next'),
                  0x0637, 0x0F3F, 0x19ED, 0x42AA, ('b', 10, 'next'),
                  ('b', None, 'done'),
                  'next', 0x3010, 0x3901, ('b', None, 'loop'),
                  'none', 0x2000,
                  'done', 0xB002, 0x4770], 0)


def town_overlay(blob):
    """Overlay 101 table entry, file ID and decompressed image (the ROM stores it BLZ-compressed)."""
    import ndspy.codeCompression
    start, size = struct.unpack_from('<II', blob, 0x50)
    rows = [(off, struct.unpack_from('<8I', blob, off)) for off in range(start, start + size, 32)]
    rows = [r for r in rows if r[1][0] == TOWN_OVERLAY]
    require(len(rows) == 1, 'Pokégear map overlay is absent', 'UNSUPPORTED_RUNTIME')
    offset, entry = rows[0]
    _, raw = cr.file_by_id(blob, entry[6])
    compressed = bool(entry[7] >> 24 & 1)
    data = ndspy.codeCompression.decompress(bytes(raw)) if compressed else bytes(raw)
    require(entry[1] == TOWN_ADDRESS and len(data) == entry[2] and entry[3] == 0,
            'Pokégear map overlay differs from the qualified build', 'UNSUPPORTED_RUNTIME')
    return {'table_offset': offset, 'entry': entry, 'file_id': entry[6], 'data': data, 'compressed': compressed}


def stock_town_specs(blob):
    info = town_overlay(blob)
    at = TOWN_SPECS - TOWN_ADDRESS
    return [SPEC.unpack_from(info['data'], at + SPEC.size * i) for i in range(TOWN_STOCK_COUNT)]


def town_bindings(blob, plan, specs, layout):
    """Created Pokégear/Town Map locations (16-byte stock-format specs) searched after the
    stock table. Overlay 101 is stored decompressed with only the 4-byte tail replaced."""
    if not specs:
        return plan
    require(len(specs) <= MAX_TOWN_SPECS and all(isinstance(v, bytes) and len(v) == SPEC.size for v in specs),
            f'At most {MAX_TOWN_SPECS} created town-map locations are supported', 'RESOURCE_CAPACITY')
    info = town_overlay(blob)
    data = bytearray(info['data'])
    tail = TOWN_TAIL - TOWN_ADDRESS
    require(bytes(data[tail:tail + 4]) == TOWN_TAIL_BEFORE and data[tail + 4:tail + 6] == b'\xf8\xbd',
            'Pokégear location lookup differs from the qualified build', 'BEFORE_VALUE_MISMATCH')
    code = layout.place('world.town-hook', len(town_hook(0, 0)), 4, 'code', called_from=TOWN_TAIL,
                        note='BL from overlay 101 0x021EA74C; returns to the stock pop')
    table = layout.place('world.town-specs', SPEC.size * len(specs), 4, 'data',
                         note=f'{len(specs)} created 16-byte location specs')
    layout.write(code, town_hook(table, len(specs)))
    layout.write(table, b''.join(specs))
    data[tail:tail + 4] = cr.thumb_bl(TOWN_TAIL, code)
    result = {**plan, 'files': dict(plan['files']), 'patches': [dict(p) for p in plan['patches']]}
    require(info['file_id'] not in result['files'], 'Another runtime edit owns overlay 101', 'RESOURCE_CONFLICT')
    result['files'][info['file_id']] = bytes(data)
    flags = info['table_offset'] + 28
    if info['compressed']:
        # Stored uncompressed: the loader reads the plain image when the compressed bit is clear.
        result['patches'].append({'rom_offset': flags, 'before': span(blob, flags, 4).hex(),
                                  'after': struct.pack('<I', 0).hex(), 'kind': 'world.town-overlay-flags'})
    result['world_town'] = {'hook': code, 'table': table, 'count': len(specs), 'call': TOWN_TAIL,
                            'overlay_file': info['file_id'], 'stored': 'uncompressed'}
    return result


def capacity(blob, plan):
    """Records that still fit after the other resident runtime additions."""
    resident = cr.overlay(blob, 129)
    used = len(plan['files'].get(resident['file_id'], resident['data']))
    free = EXTENSION_LIMIT - (used + (-used % 4) + len(hook(0, 0, 0)) + RECORD - 1)
    return max(0, min(V1_MAX_HEADERS, free // RECORD))


def decode_extension(blob):
    """Read created header records back from ROM bytes alone (ARM9 entry + overlay 129).

    Layout v2 entries are materialized with the ROM's own stock table, exactly as
    the resident hook does.
    """
    arm_start, _, _, arm_size = struct.unpack_from('<4I', blob, 0x20)
    arm = span(blob, arm_start, arm_size)
    at = BOUNDS - ARM_BASE
    if arm[at:at + len(BOUNDS_BYTES)] == BOUNDS_BYTES:
        return {'present': False, 'records': [], 'count': 0}
    first, second, target = struct.unpack_from('<2HI', arm, at)
    require((first, second) == (0x4900, 0x4708) and target & 1, 'Unknown map-header bounds entry', 'UNSUPPORTED_RUNTIME')
    address = target & ~1
    resident = cr.overlay(blob, 129)
    offset = address - resident['address']
    data = resident['data']
    from .resident import read_resident
    size = len(hook(0, 0, 0))
    v2 = len(hook_v2(0, 0, TABLE))
    v3 = len(hook_v3(0, 0, TABLE, 0))
    if 0 <= offset and offset + v3 <= len(data) and data[offset:offset + v3 - 32] == hook_v3(0, 0, TABLE, 0)[:v3 - 32]:
        base, count, entries, table, slot, identity, index, fallback = struct.unpack_from('<8I', data, offset + v3 - 32)
        require(base == BASE_COUNT and table == TABLE and fallback == FALLBACK | 1 and 0 < count <= MAX_HEADERS
                and (TABLE + index * RECORD) & 0xFFFFFFFF == slot, 'Map-header v3 literals differ', 'UNSUPPORTED_RUNTIME')
        table_bytes = read_resident(blob, entries, count * COMPACT.size)
        raw_entries = [table_bytes[COMPACT.size * k:COMPACT.size * (k + 1)] for k in range(count)]
        used = max(COMPACT_V3.unpack(e)[7] for e in raw_entries)
        require(0 < used, 'Identity overrides are absent', 'UNSUPPORTED_RUNTIME')
        override_bytes = read_resident(blob, identity, used * OVERRIDE.size)
        overrides = [override_bytes[OVERRIDE.size * k:OVERRIDE.size * (k + 1)] for k in range(used)]
        records = []
        for entry in raw_entries:
            template = COMPACT_V3.unpack(entry)[0]
            require(template < BASE_COUNT, 'Header entry has no stock template', 'UNSUPPORTED_RUNTIME')
            at = TABLE - ARM_BASE + template * RECORD
            records.append(materialize(arm[at:at + RECORD], entry, overrides))
        return {'present': True, 'version': 3, 'hook': address, 'slot': slot, 'entries': entries, 'count': count,
                'records': records, 'overrides': overrides, 'override_table': identity,
                'overlay_bytes': len(data), 'table': slot}
    if 0 <= offset and offset + v2 <= len(data) and data[offset:offset + v2 - 28] == hook_v2(0, 0, TABLE)[:v2 - 28]:
        base, count, entries, table, slot, index, fallback = struct.unpack_from('<7I', data, offset + v2 - 28)
        require(base == BASE_COUNT and table == TABLE and fallback == FALLBACK | 1 and 0 < count <= MAX_HEADERS
                and (TABLE + index * RECORD) & 0xFFFFFFFF == slot, 'Map-header v2 literals differ', 'UNSUPPORTED_RUNTIME')
        table_bytes = read_resident(blob, entries, count * COMPACT.size)
        records = []
        for k in range(count):
            entry = table_bytes[COMPACT.size * k:COMPACT.size * (k + 1)]
            template = COMPACT.unpack(entry)[0]
            require(template < BASE_COUNT, 'Header entry has no stock template', 'UNSUPPORTED_RUNTIME')
            at = TABLE - ARM_BASE + template * RECORD
            records.append(materialize(arm[at:at + RECORD], entry))
        return {'present': True, 'version': 2, 'hook': address, 'slot': slot, 'entries': entries, 'count': count,
                'records': records, 'overlay_bytes': len(data), 'table': slot}
    require(0 <= offset and offset + size <= len(data) and data[offset:offset + size - 16] == hook(0, 0, 0)[:size - 16],
            'Map-header hook differs from the qualified code', 'UNSUPPORTED_RUNTIME')
    base, count, index, fallback = struct.unpack_from('<4I', data, offset + size - 16)
    require(base == BASE_COUNT and fallback == FALLBACK | 1 and 0 < count <= V1_MAX_HEADERS,
            'Map-header hook literals differ', 'UNSUPPORTED_RUNTIME')
    table = (TABLE + index * RECORD) & 0xFFFFFFFF
    start = table - resident['address']
    require(0 <= start and start + count * RECORD <= len(data), 'Extension table is outside overlay 129', 'UNSUPPORTED_RUNTIME')
    records = [bytes(data[start + RECORD * k:start + RECORD * (k + 1)]) for k in range(count)]
    return {'present': True, 'version': 1, 'hook': address, 'table': table, 'index_base': index, 'count': count,
            'records': records, 'overlay_bytes': len(data)}


def decode_static(blob):
    """Created static members read back from ROM bytes (ARM9 stub + overlay 129)."""
    arm_start, _, _, arm_size = struct.unpack_from('<4I', blob, 0x20)
    arm = span(blob, arm_start, arm_size)
    at = STATIC_CHECK - ARM_BASE
    if arm[at:at + 4] == STATIC_BEFORE:
        return {'present': False, 'members': []}
    first, second, target = struct.unpack_from('<2HI', arm, at)
    require((first, second) == (0x4900, 0x4708) and target & 1, 'Unknown animation exclusion entry', 'UNSUPPORTED_RUNTIME')
    resident = cr.overlay(blob, 129)
    offset, data = (target & ~1) - resident['address'], resident['data']
    size = len(static_hook(0, 0))
    require(0 <= offset and offset + size <= len(data) and data[offset:offset + size - 12] == static_hook(0, 0)[:size - 12],
            'Animation exclusion hook differs from the qualified code', 'UNSUPPORTED_RUNTIME')
    stock, table, count = struct.unpack_from('<3I', data, offset + size - 12)
    start = table - resident['address']
    require(stock == STATIC_TABLE and 0 < count <= MAX_STATIC and 0 <= start and start + 2 * count <= len(data),
            'Animation exclusion literals differ', 'UNSUPPORTED_RUNTIME')
    return {'present': True, 'hook': target & ~1, 'table': table,
            'members': list(struct.unpack_from(f'<{count}H', data, start))}


def decode_town(blob):
    """Created Pokégear/Town Map specs read back from ROM bytes (overlay 101 + overlay 129)."""
    info = town_overlay(blob)
    tail = TOWN_TAIL - TOWN_ADDRESS
    code = info['data'][tail:tail + 4]
    if code == TOWN_TAIL_BEFORE:
        return {'present': False, 'specs': [], 'compressed': info['compressed']}
    first, second = struct.unpack('<HH', code)
    target = _bl_target(TOWN_TAIL, first, second)
    require(target is not None, 'Unknown Pokégear lookup tail', 'UNSUPPORTED_RUNTIME')
    resident = cr.overlay(blob, 129)
    offset, data = target - resident['address'], resident['data']
    size = len(town_hook(0, 0))
    require(0 <= offset and offset + size <= len(data) and data[offset:offset + size - 8] == town_hook(0, 0)[:size - 8],
            'Pokégear location hook differs from the qualified code', 'UNSUPPORTED_RUNTIME')
    table, count = struct.unpack_from('<2I', data, offset + size - 8)
    start = table - resident['address']
    require(0 < count <= MAX_TOWN_SPECS and 0 <= start and start + SPEC.size * count <= len(data),
            'Pokégear location literals differ', 'UNSUPPORTED_RUNTIME')
    specs = [SPEC.unpack_from(data, start + SPEC.size * k) for k in range(count)]
    return {'present': True, 'hook': target, 'table': table, 'specs': specs, 'compressed': info['compressed']}
