"""Custom healing/blackout destinations and Fly destinations (TRAVEL-01, FIELD-05).

Qualified against the pinned ROM and pret/pokeheartgold 9d8b759 (asm/unk_0203BA5C.s,
src/blackout.c, src/application/pokegear/map/*, src/sys_flags.c; ledger
work/original-content-v1/impl/LEDGER.md):

* The stock spawn table sSpawnMaps (ARM9 0x020F9E80, 30 rows of 18 bytes: flypoint flag
  index | blackout bit 8 | fly bit 9, death map, death x | z << 8, fly map/x/z, special
  map/x/z) is read only through eleven literal words in ARM9 0x0203BA5C..0x0203BBB0, and
  its row count is bounded by four ``cmp rN, #0x1E`` (SpawnIdToTableIndex, the death-map
  lookup run on every map entry, the fly-map lookup used by Fly and FlypointFlagAction run
  on every map entry). No other ARM9/overlay word points into the table. An extended copy
  (stock rows unchanged, authored rows appended from spawn 31) lives in the resident boot
  data region (resident.BOOT_REGION), because blackout reads it while the field overlays
  may be unloaded; Teleport lands on the blackout row's fly tile (overlay 2 0x0224C840).
* Entering a row's death map with the blackout bit set makes it the blackout point, as a
  Pokémon Center does; the ``set_spawn`` step selects it explicitly. After a loss,
  Task_Blackout heals, warps to the death map/tile and queues std 2013 (the nurse
  script, which only knows stock Pokémon Centers). One BL in Task_Blackout (0x0205295C)
  is redirected to a resident routine that queues the respawn map's own arrival script
  (a local script this module adds: fade in, walking state as std 2013, the author's
  pages) for authored spawn IDs and std 2013 otherwise.
* Fly: entering a row's fly map with the fly bit set sets flypoint flag 0x9B0 + index
  (Save_VarsFlags_FlypointFlagAction asserts index < 38). The Pokégear fly map
  (overlay 101) lists gMapFlypointParams (0x021F79B4, 27 rows of 14 bytes) in five loops
  bounded by ``cmp rN, #0x1B`` and five literal words, with one marker sprite per row in a
  42-object manager. Authored Fly rows are appended to an extended copy (boot data),
  the bounds and the manager size grow with them, and overlay 101 is stored decompressed
  (as world_runtime.town_bindings already does). Choosing a marker returns its warp map,
  whose spawn row supplies the landing tile (GetFlyWarpData), exactly like stock towns.
* Choosing a Fly marker names it through PokegearMap_GetLocationSpecByMapID (overlay 101
  0x021EA758), which searched only the stock specs: for an authored row (a created header) it
  returned NULL and ov101_021EB784 then read the map ID through that NULL pointer
  (0x021EB7FA, R101-FLY). With authored Fly rows its not-found tail (``movs r0,#0; pop
  {r4-r6,pc}`` at 0x021EA78C) becomes a BL to a resident search of the created town-map specs
  by map ID that ends with the same pop; stock specs still win. Projects without authored Fly
  rows keep their overlay 101 bytes.
* Authored fly rows use flypoint indices no stock consumer uses: no spawn row, fly-map
  row, script flag operand, event flag, code constant or played save touches flags
  0x9C7, 0x9C8, 0x9CA, 0x9D0 or 0x9D2 (qualify_flags; 0x9CA/0x9D2 appear only inside a
  script member's entry-offset table). Five Fly destinations, never reused once given.

A point is never deleted: its spawn ID and flag may be stored in saves, so ``retire``
clears its blackout/fly bits and removes its Pokégear marker but keeps the row. Pure
planning and candidate bytes; Project owns writes.
"""
import copy
import re
import struct

from . import world
from .formats import EditorError, require, span

SCHEMA = 'sovereign-travel-point-transaction-v1'
ACTIONS = ('define', 'revise', 'retire')
FIELDS = ('name', 'arrival', 'respawn', 'blackout', 'fly', 'message')
KEY = re.compile(r'[a-z][a-z0-9_]{0,23}')
ARM_BASE = 0x02000000
MAX_POINTS = 32

SPAWN_TABLE, STOCK_SPAWNS, SPAWN_SIZE = 0x020F9E80, 30, 18
SPAWN_LITERALS = ((0x0203BAA0, 6), (0x0203BAA4, 8), (0x0203BAA8, 10), (0x0203BAE0, 2), (0x0203BAE4, 0),
                  (0x0203BB14, 12), (0x0203BB18, 14), (0x0203BB1C, 16), (0x0203BB4C, 0), (0x0203BB6C, 0),
                  (0x0203BBB0, 0))
# (address, high byte of ``cmp rN, #imm``): r0 SpawnIdToTableIndex, r2 death lookup, r2 fly lookup, r4 flags.
SPAWN_BOUNDS = ((0x0203BA62, 0x28), (0x0203BB42, 0x2A), (0x0203BB62, 0x2A), (0x0203BBA8, 0x2C))
BLACKOUT_QUEUE, QUEUE_SCRIPT = 0x0205295C, 0x0203FED4
LOCAL_FIELD_DATA, BLACKOUT_SPAWN = 0x0203B9C4, 0x0203B994
WHITED_OUT = 2013
FLY_OVERLAY, FLY_TABLE, STOCK_FLYPOINTS, FLY_SIZE = 101, 0x021F79B4, 27, 14
FLY_LITERALS = ((0x021EA5FC, 0), (0x021EA870, 0), (0x021EA8A4, 2), (0x021EA98C, 0), (0x021EB1D8, 0))
FLY_BOUNDS = ((0x021E9C66, 0x2D), (0x021EA5F2, 0x2F), (0x021EA864, 0x2F), (0x021EB092, 0x2F), (0x021EE5E4, 0x2E))
FLY_OBJECTS, FLY_OBJECT_COUNT = 0x021EE3A8, 42
# PokegearMap_GetLocationSpecByMapID not-found tail: movs r0,#0; pop {r4,r5,r6,pc}.
NAME_TAIL, NAME_TAIL_BEFORE = 0x021EA78C, bytes.fromhex('002070bd')
FLYPOINT_FLAGS, FLYPOINT_LIMIT = 0x9B0, 38
FLY_FLAGS = (0x17, 0x18, 0x1A, 0x20, 0x22)
# Arrival script (the respawn map's own local script): std 2013's fade and walking state.
LOCK, RELEASE, END, FADE, WAIT_FADE = 96, 97, 2, 174, 175
PLAYER_STATE, AVATAR_BITS, AVATAR_UPDATE, RESULT = 187, 188, 189, 0x800C
WALKING, ROCKET_WALKING, ROCKET_STATE = 1, 1024, 3


def is_transaction(t):
    return isinstance(t, dict) and t.get('schema') == SCHEMA


def points(state):
    return (state.get('travel') or {}).get('points', {})


def integer(v, lo, hi):
    return type(v) is int and not isinstance(v, bool) and lo <= v <= hi


# ---- qualification ---------------------------------------------------------------------------

def _arm(blob):
    start, _, _, size = struct.unpack_from('<4I', blob, 0x20)
    return start, span(blob, start, size)


def stock_spawn_rows(blob):
    _, arm = _arm(blob)
    at = SPAWN_TABLE - ARM_BASE
    return [bytes(arm[at + SPAWN_SIZE * i:at + SPAWN_SIZE * (i + 1)]) for i in range(STOCK_SPAWNS)]


def stock_fly_rows(data):
    at = FLY_TABLE - world_runtime_address()
    return [bytes(data[at + FLY_SIZE * i:at + FLY_SIZE * (i + 1)]) for i in range(STOCK_FLYPOINTS)]


def world_runtime_address():
    from . import world_runtime
    return world_runtime.TOWN_ADDRESS


def qualify_runtime(blob, fly_image=None):
    """Exact before-values of every patched site (ARM9 and, when given, overlay 101)."""
    _, arm = _arm(blob)
    for address, offset in SPAWN_LITERALS:
        require(struct.unpack_from('<I', arm, address - ARM_BASE)[0] == SPAWN_TABLE + offset,
                f'Spawn-table literal at {address:#x} differs from the qualified build', 'BEFORE_VALUE_MISMATCH')
    for address, high in SPAWN_BOUNDS:
        require(bytes(arm[address - ARM_BASE:address - ARM_BASE + 2]) == bytes((STOCK_SPAWNS, high)),
                f'Spawn bound at {address:#x} differs from the qualified build', 'BEFORE_VALUE_MISMATCH')
    from . import character_runtime as cr
    require(bytes(arm[BLACKOUT_QUEUE - ARM_BASE:BLACKOUT_QUEUE - ARM_BASE + 4]) == cr.thumb_bl(BLACKOUT_QUEUE, QUEUE_SCRIPT),
            'Blackout script call differs from the qualified build', 'BEFORE_VALUE_MISMATCH')
    literal = BLACKOUT_QUEUE + 0x1C   # the pool word holding 2013 (ldr r1 at 0x02052956)
    require(struct.unpack_from('<I', arm, literal - ARM_BASE)[0] == WHITED_OUT,
            'Blackout script ID differs from the qualified build', 'BEFORE_VALUE_MISMATCH')
    if fly_image is not None:
        base = world_runtime_address()
        for address, offset in FLY_LITERALS:
            require(struct.unpack_from('<I', fly_image, address - base)[0] == FLY_TABLE + offset,
                    f'Fly-map literal at {address:#x} differs from the qualified build', 'BEFORE_VALUE_MISMATCH')
        for address, high in FLY_BOUNDS:
            require(bytes(fly_image[address - base:address - base + 2]) == bytes((STOCK_FLYPOINTS, high)),
                    f'Fly-map bound at {address:#x} differs from the qualified build', 'BEFORE_VALUE_MISMATCH')
        require(bytes(fly_image[FLY_OBJECTS - base:FLY_OBJECTS - base + 2]) == bytes((FLY_OBJECT_COUNT, 0x20)),
                'Fly-map object count differs from the qualified build', 'BEFORE_VALUE_MISMATCH')


def qualify_flags(project):
    """Evidence that the authored flypoint indices have no stock consumer (cached per ROM)."""
    cache = project.__dict__.get('_travel_flags')
    if cache is not None and cache[0] is project.blob:
        return cache[1]
    import ndspy.narc
    from . import script_disasm as sd
    from .formats import file_span
    blob = project.blob
    used_spawn = {row[0] for row in stock_spawn_rows(blob)}
    from . import world_runtime
    fly = world_runtime.town_overlay(blob)['data']
    used_fly = {row[4] for row in stock_fly_rows(fly)}
    flags = {FLYPOINT_FLAGS + i: i for i in FLY_FLAGS}
    operands = {}
    for archive in ('a/0/1/2',):
        for member, raw in enumerate(ndspy.narc.NARC(bytes(file_span(blob, archive)[1])).files):
            if not any(struct.pack('<H', f) in raw for f in flags):
                continue
            for at, (op, name, args, _) in sd.disassemble(raw).items():
                for a in args:
                    if a in flags:
                        operands.setdefault(a, []).append(f'{archive}[{member}]@{at} {name}')
    npc_flags = set()
    for raw in ndspy.narc.NARC(bytes(file_span(blob, world.EVENT_ARCHIVE)[1])).files:
        cursor = 0
        for size in (20, 32, 12, 16):
            n = struct.unpack_from('<I', raw, cursor)[0]
            cursor += 4
            for k in range(n):
                if size == 32:
                    npc_flags.add(struct.unpack_from('<H', raw, cursor + k * size + 8)[0])
            cursor += n * size
    rows = {}
    for flag, index in flags.items():
        problems = []
        if index in used_spawn:
            problems.append('stock spawn row')
        if index in used_fly:
            problems.append('stock fly-map row')
        if flag in operands:
            problems.append('script operand ' + ', '.join(operands[flag][:3]))
        if flag in npc_flags:
            problems.append('event flag')
        rows[f'{index:#x}'] = {'flag': f'{flag:#x}', 'problems': problems}
    report = {'indices': [i for i in FLY_FLAGS if not rows[f'{i:#x}']['problems']], 'rows': rows,
              'note': 'Code constants (every BL to Save_VarsFlags_FlypointFlagAction 0x02066930 with a constant '
                      'index: 5, 15-19, 21, 25, 27) and 23 played saves were reviewed in the ledger.'}
    project.__dict__['_travel_flags'] = (project.blob, report)
    return report


# ---- authoring ---------------------------------------------------------------------------------

def _tile_spec(value, label, limit=65535):
    require(isinstance(value, dict) and set(value) == {'header', 'x', 'z'} and integer(value['header'], 0, 65534)
            and integer(value['x'], 0, limit) and integer(value['z'], 0, limit),
            f'{label} needs header, x and z (global tiles{", 0..255" if limit == 255 else ""})', 'INVALID_INPUT')
    return {'header': value['header'], 'x': value['x'], 'z': value['z']}


def _town_cell(project, state, header):
    """Pokégear fly-map cell (x, row) of an arrival header: its created-area town marker, or
    the stock location spec of a stock header."""
    from . import world_identity, world_runtime, world_authoring
    for key, area in world_authoring.areas(state).items():
        if area['header'] == header:
            town = (world_identity.identities(state).get(key) or {}).get('town_map')
            require(town, 'A Fly destination needs the area’s Pokégear position (World → Identity → Town map)',
                    'INVALID_DESTINATION')
            return town['x'], town['y'] - 2
    for spec in world_runtime.stock_town_specs(project.blob):
        if spec[0] == header:
            return spec[1], spec[2] - 2
    require(False, f'Header {header} has no Pokégear location to show a Fly marker', 'INVALID_DESTINATION')


def _normalise(project, state, spec):
    out = {'name': spec.get('name'), 'arrival': _tile_spec(spec.get('arrival'), 'Arrival'),
           'respawn': None if spec.get('respawn') is None else _tile_spec(spec['respawn'], 'Respawn', 255),
           'blackout': spec.get('blackout', False), 'fly': spec.get('fly', False),
           'message': list(spec.get('message') or [])}
    require(isinstance(out['name'], str) and 0 < len(out['name']) <= 24, 'A travel point needs a name (<= 24)',
            'INVALID_INPUT')
    require(type(out['blackout']) is bool and type(out['fly']) is bool, 'blackout and fly are true/false',
            'INVALID_INPUT')
    require(out['blackout'] or out['fly'], 'A travel point is a respawn point, a Fly destination or both',
            'INVALID_INPUT')
    require(not out['message'] or out['blackout'], 'Only a respawn point has an arrival message', 'INVALID_INPUT')
    if out['message']:
        from . import dialogue_format as fmt
        fmt.encode_pages(out['message'])
    if out['blackout'] and out['respawn'] is None:
        a = out['arrival']
        require(a['x'] <= 255 and a['z'] <= 255, 'The arrival tile is beyond 255; give a respawn tile (0..255)',
                'INVALID_INPUT')
    return out


def _qualify(project, state, key, point):
    """Destination rules first (one point per arrival map, fly-map conflicts), then the
    arrival/respawn tiles (clear, with follower room) and private respawn script banks."""
    from . import travel
    header = point['arrival']['header']
    # One point per arrival map: the fly lookup (sub_0203BB50) takes the first row whose fly map
    # matches, and Teleport lands on the blackout row's fly tile, so rows must not share it.
    for other, p in points(state).items():
        require(other == key or p['arrival']['header'] != header,
                f'Travel point {other} already arrives in this map; revise it instead', 'INVALID_DESTINATION')
    rows = {}
    if point['fly']:
        from . import world_runtime
        stock = world_runtime.town_overlay(project.blob)['data']
        require(not any(struct.unpack_from('<H', row, 2)[0] == header for row in stock_fly_rows(stock)),
                'This map is already a stock Fly destination', 'INVALID_DESTINATION')
        require(not any(struct.unpack_from('<H', row, 6)[0] == header for row in stock_spawn_rows(project.blob)),
                'A stock spawn row already lands in this map', 'INVALID_DESTINATION')
        x, row = _town_cell(project, state, header)
        from .world_identity import region_of
        rows['pokegear'] = {'x': x, 'row': row, 'region': region_of(x, row + 2)}
    rows['arrival'] = travel.arrival(project, state, header, point['arrival']['x'], point['arrival']['z'], 1)
    if point['blackout']:
        r = point['respawn'] or point['arrival']
        rows['respawn'] = travel.arrival(project, state, r['header'], r['x'], r['z'], 0)
        head = project.header(r['header'])
        from .area_layout import resource_users
        cell = _cell(project, r['header'], r['x'], r['z'])
        users = resource_users(project, project.context(header=r['header'], cell=cell))
        require(users['scripts'] == [r['header']] and users['texts'] == [r['header']],
                f"Respawn map {head['name']} shares its script or text bank; choose an area with its own",
                'SHARED_RESOURCE')
    return rows


def _cell(project, header, x, z):
    from .border_authoring import cell_of
    try:
        return list(cell_of(project, header, x, z))
    except EditorError as exc:
        raise EditorError('INVALID_DESTINATION', f'Tile {x},{z} is outside header {header}') from exc


def plan(project, state, index, action, key=None, label=None, **fields):
    require(action in ACTIONS, f"Travel point action is one of {', '.join(ACTIONS)}", 'INVALID_INPUT')
    require(isinstance(key, str) and KEY.fullmatch(key), 'A travel point needs a key (lowercase, <= 24)',
            'INVALID_INPUT')
    require(label is None or isinstance(label, str) and 0 < len(label) <= 160, 'Invalid label', 'INVALID_INPUT')
    require(set(fields) <= set(FIELDS), f"Travel point fields are {', '.join(FIELDS)}", 'INVALID_INPUT')
    current = points(state)
    before = copy.deepcopy(current.get(key))
    if action == 'define':
        require(before is None, f'Travel point {key} already exists; revise it', 'EXISTS')
        require(len(current) < MAX_POINTS, f'At most {MAX_POINTS} travel points', 'RESOURCE_CAPACITY')
        after = {**_normalise(project, state, fields), 'spawn': STOCK_SPAWNS + 1 + len(current), 'flag': None,
                 'retired': False}
    else:
        require(before is not None, f'No travel point {key}', 'NOT_FOUND')
        require(not before['retired'], f'Travel point {key} is retired', 'RETIRED')
        if action == 'retire':
            require(not fields, 'Retire takes only the key', 'INVALID_INPUT')
            users = spawn_users(state, key)
            require(not users, f"Events still select this respawn point: {', '.join(users)}", 'IN_USE')
            after = {**before, 'retired': True}
        else:
            require(fields, 'Name the fields to change', 'INVALID_INPUT')
            merged = {k: before[k] for k in FIELDS}
            merged.update(copy.deepcopy(fields))
            after = {**before, **_normalise(project, state, merged)}
            require(after != before, 'The travel point is unchanged', 'NO_CHANGE')
            require(after['blackout'] or not spawn_users(state, key),
                    'Events select this respawn point; keep it a respawn point', 'IN_USE')
    report = None
    if not after['retired']:
        if after['fly'] and after['flag'] is None:
            held = {p['flag'] for p in current.values() if p.get('flag') is not None}
            free = [i for i in qualify_flags(project)['indices'] if i not in held]
            require(free, f'All {len(FLY_FLAGS)} qualified Fly destination slots are in use', 'RESOURCE_CAPACITY')
            after['flag'] = free[0]
        report = _qualify(project, state, key, after)
    return {'schema': SCHEMA, 'index': index, 'action': action, 'key': key,
            'label': label or f'{action.capitalize()} travel point {key}',
            'request': {'action': action, 'key': key, 'label': label, **copy.deepcopy(fields)},
            'before': before, 'after': after, 'report': report}


def replay(project, state, t, index):
    try:
        expected = plan(project, state, index, **t['request'])
    except (KeyError, TypeError) as exc:
        raise EditorError('INVALID_INPUT', 'Malformed travel point transaction') from exc
    require(expected == t, 'Travel point before-value or qualification differs', 'BEFORE_VALUE_MISMATCH')
    state.setdefault('travel', {}).setdefault('points', {})[t['key']] = copy.deepcopy(t['after'])


def summary(t):
    return {'operation': 'travel.transaction', 'index': t['index'], 'action': t['action'], 'key': t['key'],
            'label': t['label'], 'before': t['before'], 'after': t['after']}


def spawn_users(state, key):
    from .story_authoring import catalog
    return sorted(k for k, s in catalog(state, 'sequence').items()
                  if any(n.get('op') == 'set_spawn' and n.get('point') == key for n in s.get('nodes', [])))


def spawn_id(state, key):
    p = points(state).get(key)
    require(p is not None and not p['retired'] and p['blackout'], f'No active respawn point {key}', 'NOT_FOUND')
    return p['spawn']


def view(project, state):
    rows = []
    for key, p in sorted(points(state).items(), key=lambda kv: kv[1]['spawn']):
        rows.append({'key': key, **copy.deepcopy(p), 'arrival_name': project.header(p['arrival']['header'])['name']})
    return {'points': rows, 'capacity': {'points': [len(rows), MAX_POINTS],
                                         'fly': [sum(p['flag'] is not None for p in points(state).values()),
                                                 len(qualify_flags(project)['indices'])]},
            'notes': ['Spawn IDs and Fly flags are permanent: saves may hold them, so points retire instead of '
                      'being deleted.',
                      'A respawn point becomes the blackout point when the player enters its respawn map '
                      '(like a Pokémon Center) or an event runs a Respawn step for it.',
                      'A Fly destination is discovered on the first visit to its arrival map and needs the '
                      'Storm Badge, like stock towns.']}


# ---- arrival scripts ---------------------------------------------------------------------------

def arrival_scripts(project, state):
    """(key, script member, text member, pages) of every respawn point, in spawn order."""
    rows = []
    for key, p in sorted(points(state).items(), key=lambda kv: kv[1]['spawn']):
        if not p['blackout']:
            continue
        r = p['respawn'] or p['arrival']
        head = project.header(r['header'])
        rows.append((key, head['script_file'], head['text_archive'], list(p['message'])))
    return rows


def compile_arrival(first_message, count):
    """Std 2013's fade-in and walking state, then the author's pages (no nurse)."""
    from .scene_commands import Code
    c = Code()
    c.emit('H', 609)
    c.emit('H', LOCK)
    c.emit('5H', FADE, 6, 1, 1, 0)
    c.emit('H', WAIT_FADE)
    c.emit('2H', PLAYER_STATE, RESULT)
    c.compare(RESULT, ROCKET_STATE)
    c.jump('$rocket', 1)
    c.emit('2H', AVATAR_BITS, WALKING)
    c.jump('$update')
    c.label('$rocket')
    c.emit('2H', AVATAR_BITS, ROCKET_WALKING)
    c.label('$update')
    c.emit('H', AVATAR_UPDATE)
    for i in range(count):
        c.emit('HB', 45, first_message + i)
        c.emit('2H', 49, 53)
    c.emit('H', RELEASE)
    c.emit('H', END)
    return c.finish()


# ---- runtime ---------------------------------------------------------------------------------

def spawn_row(p):
    """18-byte row: flag | blackout << 8 | fly << 9, death map, x | z << 8, fly/special map/x/z."""
    active = not p['retired']
    word = (p['flag'] or 0) | (active and p['blackout']) << 8 | (active and p['fly']) << 9
    r = p['respawn'] or p['arrival']
    a = p['arrival']
    return struct.pack('<3H6H', word, r['header'], r['x'] | r['z'] << 8, a['header'], a['x'], a['z'],
                       a['header'], a['x'], a['z'])


def fly_row(project, state, p):
    x, row = _town_cell(project, state, p['arrival']['header'])
    header = p['arrival']['header']
    # unk_05 0xFF: no undiscovered-town tile overlay (as Indigo Plateau and the special rows).
    return struct.pack('<HH9Bx', header, header, p['flag'], 0xFF, x, row, 0, 0, 0x11, 0, 0)


def hook(address, table, count):
    """Resident Thumb routine replacing Task_Blackout's BL QueueScript(2013): authored spawn IDs
    queue their respawn map's arrival script (a local ID from ``table``), others keep 2013."""
    from . import resident
    return resident.thumb([
        0xB51F,                    # push {r0-r4, lr}
        0x68E8,                    # ldr r0, [r5, #0xC]   (FieldSystem.saveData; r5 = fieldSystem)
        ('bl', LOCAL_FIELD_DATA),
        ('bl', BLACKOUT_SPAWN),
        0x381F,                    # subs r0, #31
        0x2800 | count,            # cmp r0, #count
        ('b', 2, 'stock'),         # bhs stock (stock IDs 1..30 wrap below zero)
        0x0040,                    # lsls r0, r0, #1
        ('ldr', 1, table),
        0x5A08,                    # ldrh r0, [r1, r0]
        0x2800,                    # cmp r0, #0
        ('b', 0, 'stock'),         # beq stock
        0x9001,                    # str r0, [sp, #4]     (saved r1 = script ID)
        'stock',
        0xBC0F,                    # pop {r0-r3}
        ('bl', QUEUE_SCRIPT),
        0xBD10,                    # pop {r4, pc}
    ], address)


def name_hook(specs, count):
    """Thumb reached by BL from the not-found tail of PokegearMap_GetLocationSpecByMapID
    (r1 = map ID, stack frame {r4-r6, lr}). Returns the first created spec whose map ID
    matches, else NULL, and ends with the stock ``pop {r4,r5,r6,pc}``."""
    from . import resident
    return resident.thumb([('ldr', 0, specs), ('ldr', 2, count),
                           'loop', 0x2A00, ('b', 0, 'none'),      # cmp r2,#0; beq none
                           0x8803, 0x428B, ('b', 0, 'done'),      # ldrh r3,[r0]; cmp r3,r1; beq done
                           0x3010, 0x3A01, ('b', None, 'loop'),   # adds r0,#16; subs r2,#1; b loop
                           'none', 0x2000,                        # movs r0,#0
                           'done', 0xBD70], 0)                    # pop {r4,r5,r6,pc}


def bindings(project, state, plan, layout, script_ids):
    """Extended spawn and fly tables, blackout hook and fly-map patches (layout v2 only)."""
    rows = [p for p in sorted(points(state).values(), key=lambda p: p['spawn'])]
    if not rows:
        return plan
    require(layout is not None, 'Travel points need the resident layout', 'UNSUPPORTED_RUNTIME')
    blob = project.blob
    from . import character_runtime as cr, world_runtime
    info = world_runtime.town_overlay(blob)
    fly = [p for p in rows if p['fly'] and not p['retired']]
    qualify_runtime(blob, info['data'] if fly else None)
    result = {**plan, 'files': dict(plan['files']), 'patches': [dict(p) for p in plan['patches']]}
    arm_start, _ = _arm(blob)

    def arm_patch(address, before, after, kind):
        offset = arm_start + address - ARM_BASE
        require(bytes(span(blob, offset, len(before))) == before, f'{kind} before-value differs', 'BEFORE_VALUE_MISMATCH')
        require(not any(p['rom_offset'] == offset for p in result['patches']), f'{kind}: site already patched',
                'RESOURCE_CONFLICT')
        result['patches'].append({'rom_offset': offset, 'before': before.hex(), 'after': after.hex(), 'kind': kind})

    total = STOCK_SPAWNS + len(rows)
    require(all(p['spawn'] == STOCK_SPAWNS + 1 + i for i, p in enumerate(rows)), 'Spawn IDs are not dense',
            'STALE_EDIT')
    table = layout.place_boot('travel.spawns', SPAWN_SIZE * total, 4, note=f'{total} spawn rows (30 stock)')
    layout.write(table, b''.join(stock_spawn_rows(blob)) + b''.join(spawn_row(p) for p in rows))
    for address, offset in SPAWN_LITERALS:
        arm_patch(address, struct.pack('<I', SPAWN_TABLE + offset), struct.pack('<I', table + offset), 'travel.spawn-literal')
    for address, high in SPAWN_BOUNDS:
        arm_patch(address, bytes((STOCK_SPAWNS, high)), bytes((total, high)), 'travel.spawn-bound')
    scripts = struct.pack(f'<{len(rows)}H', *[script_ids.get(p['spawn'], 0) for p in rows])
    table_ids = layout.place_boot('travel.arrival-scripts', len(scripts), 4, note='local script ID per authored spawn')
    layout.write(table_ids, scripts)
    code = layout.place('travel.blackout-hook', len(hook(0, 0, 0)), 4, 'code', called_from=BLACKOUT_QUEUE,
                        note='BL from Task_Blackout 0x0205295C; queues the respawn map arrival script')
    layout.write(code, hook(code, table_ids, len(rows)))
    arm_patch(BLACKOUT_QUEUE, cr.thumb_bl(BLACKOUT_QUEUE, QUEUE_SCRIPT), cr.thumb_bl(BLACKOUT_QUEUE, code),
              'travel.blackout-hook')
    report = {'spawn_table': table, 'rows': total, 'hook': code, 'arrival_scripts': table_ids}
    if fly:
        data = bytearray(result['files'].get(info['file_id'], info['data']))
        stock = stock_fly_rows(info['data'])
        rows_fly = stock + [fly_row(project, state, p) for p in fly]
        fly_table = layout.place_boot('travel.flypoints', FLY_SIZE * len(rows_fly), 4,
                                      note=f'{len(rows_fly)} Pokégear fly rows (27 stock)')
        layout.write(fly_table, b''.join(rows_fly))
        base = world_runtime_address()
        for address, offset in FLY_LITERALS:
            struct.pack_into('<I', data, address - base, fly_table + offset)
        for address, high in FLY_BOUNDS:
            data[address - base:address - base + 2] = bytes((len(rows_fly), high))
        count = FLY_OBJECT_COUNT + len(fly)
        require(count < 111, 'The fly map sprite manager is limited to the gear map size', 'RESOURCE_CAPACITY')
        data[FLY_OBJECTS - base:FLY_OBJECTS - base + 2] = bytes((count, 0x20))
        # R101-FLY: authored rows are named through the created town-map specs.
        town = result.get('world_town')
        require(town and town['count'], 'A Fly destination needs its created Pokégear location', 'UNSUPPORTED_RUNTIME')
        tail = NAME_TAIL - base
        require(bytes(data[tail:tail + 4]) == NAME_TAIL_BEFORE,
                'Pokégear name lookup differs from the qualified build', 'BEFORE_VALUE_MISMATCH')
        name = layout.place('travel.fly-name-hook', len(name_hook(0, 0)), 4, 'code', called_from=NAME_TAIL,
                            note='BL from overlay 101 0x021EA78C; created specs by map ID')
        layout.write(name, name_hook(town['table'], town['count']))
        data[tail:tail + 4] = cr.thumb_bl(NAME_TAIL, name)
        result['files'][info['file_id']] = bytes(data)
        flags = info['table_offset'] + 28
        if info['compressed'] and not any(p['rom_offset'] == flags for p in result['patches']):
            result['patches'].append({'rom_offset': flags, 'before': span(blob, flags, 4).hex(),
                                      'after': struct.pack('<I', 0).hex(), 'kind': 'travel.fly-overlay-flags'})
        report.update(fly_table=fly_table, fly_rows=len(rows_fly), fly_objects=count, name_hook=name)
    result['travel'] = report
    return result
