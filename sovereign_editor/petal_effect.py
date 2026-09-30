"""Airborne blossom petals: one area-scoped field weather (EFFECT-01, reference CG-15).

Qualified against the pinned ROM and pret/pokeheartgold 9d8b759 (asm/overlay_01_021EB1E8.s,
src/field/fieldmap.c, src/field_warp_tasks.c; ledger work/original-content-v1/impl/LEDGER.md):

* Field weather is the header's weather field. A warp copies it into the saved local field data
  and the field calls WeatherManager_SetWeather / ChangeWeather (overlay 1); crossing into another
  header changes it the same way, and a reload restores it from the save. Every weather keeps a
  28-byte descriptor in a 14-entry table (0x022098B0, .data reloaded with overlay 1): sprite set,
  BG set, work size, runtime state and a SysTask function. The sprite sets come from four resource
  lists in a/0/6/3 (members 55..58: cell, animation, character, palette); a running weather owns a
  64-sprite particle pool.
* Petals are weather 14. Overlay 1 learns a 15th weather: the table moves to the boot data region
  (WeatherManager_New's call to the loader copies the freshly loaded stock rows and the petal row
  there, so every field load starts from stock values), four bounds grow from 14 to 15, and the
  manager's "no pending weather" sentinel moves from 14 to 0xFF. a/0/6/3 gains the petal
  character tiles and palette as sprite set 10 (resource id 1010), reusing snow's cells and
  animation (four 8x8 frames).
* The petal task is snow's own state machine (ov01_021ECD08) relocated byte for byte, with its
  fog tint switched off and its spawn and motion callbacks replaced. The spawn callback looks the
  current map up in a resident table: density sets how many petals are spawned per spawner tick
  (the ramp-up/fade-out and 64-sprite pool are stock), speed and direction set each petal's
  drift, lifetime and spin; each petal starts on a random frame and advances one frame per spin
  step (\\ | / -). Motion wraps like snow (a 319 x 256 virtual field) and petals end below the
  screen.
* Everything else that reads the weather accepts 14 as "no weather": battle setup (hg-engine
  switches default to none), Snow Cloak (weather 5), Flash/Defog (11, 9), the encounter and
  sweet-scent readers in overlay 2. Headers whose weather drives lighting (11, 12, 13) refuse
  petals.

The effect draws on the weather sprite layer: over the map, player and follower like snow; it
never changes map models, so canopies, ground piles and water animation are untouched.
Pure candidate bytes; Project owns writes.
"""
import copy
import functools
import math
import struct
from pathlib import Path

from .formats import EditorError, member_count, require, resource

SCHEMA = 'sovereign-petal-effect-transaction-v1'
ACTIONS = ('set', 'remove')
FIELDS = ('density', 'speed', 'direction')
LIMITS = {'density': (1, 10), 'speed': (1, 10), 'direction': (-60, 60)}
DEFAULT = {'density': 4, 'speed': 4, 'direction': -20}
MAX_AREAS = 64
WEATHER = 14
LIGHTING = {11: 'dark cave (Flash)', 12: 'lit cave (Flash)', 13: 'low light'}
POOL = 64                                  # stock weather particle pool (sprites)

OVERLAY_FILE, OVERLAY_BASE = 1, 0x021E5900
STOCK_TABLE, STOCK_ROWS, ROW = 0x022098B0, 14, 28
TABLE_LITERAL = 0x021EB684                 # ov01_021EB64C: ldr r0, =ov01_022098B0
LOADER, LOADER_CALL = 0x021EB64C, 0x021EB212
# (address, stock halfword, petal halfword)
HALFWORDS = (
    (0x021EB270, 0x2C0E, 0x2C0F),          # WeatherManager_SetWeather: weather < 15
    (0x021EB2BE, 0x2C0E, 0x2C0F),          # WeatherManager_ChangeWeather: weather < 15
    (0x021EB6A4, 0x2D0E, 0x2D0F),          # WeatherManager_Delete: unload 15 weathers
    (0x021EB804, 0x290E, 0x290F),          # ov01_021EB804 (status): weather < 15
    (0x021EB224, 0x200E, 0x20FF),          # WeatherManager_New: no pending weather = 0xFF
    (0x021EB3E0, 0x290E, 0x29FF),          # ov01_021EB320: pending?
    (0x021EB3EA, 0x200E, 0x20FF),
    (0x021EB4A4, 0x290E, 0x29FF),          # ov01_021EB3F0: pending?
    (0x021EB4AE, 0x200E, 0x20FF),
)
DISPATCH_BOUND = (0x021EB708, 0x2C0E)      # ov01_021EB700: weather <= 14 already (ble)

# Snow's task (ov01_021ECD08 .. literal pool end) and the call sites it makes.
SNOW_TASK, SNOW_END = 0x021ECD08, 0x021ECF4C
SNOW_SPAWN, SNOW_MOTION = 0x021ECF4C, 0x021ED070
SNOW_CALLS = {0x021ECD50: 0x021EC504, 0x021ECD7A: 0x021EC5FC, 0x021ECD96: 0x021EC538, 0x021ECDC0: 0x021EC650,
              0x021ECDF2: 0x021EC504, 0x021ECE10: 0x021EC678, 0x021ECE18: 0x021EC7C8, 0x021ECE32: 0x021EC85C,
              0x021ECE50: SNOW_SPAWN, 0x021ECE70: 0x021EC52C, 0x021ECE84: 0x021EC790, 0x021ECE98: 0x021EC538,
              0x021ECEC2: 0x021EC7AC, 0x021ECEF8: 0x021EA864, 0x021ECEFE: 0x021EBCA4, 0x021ECF14: 0x021EC2E4,
              0x021ECF1E: 0x021EC470, 0x021ECF24: 0x021EC300}
TINT_SETUP, TINT_WAIT = 0x021ECD7A, 0x021ECDC0      # bl ov01_021EC5FC / bl ov01_021EC650
TINT_FLAGS = (0x021ECDF8, 0x021ECE76, 0x021ECEB8, 0x021ECEE6)   # ldrh r0, [r5, r0] of state+0xF64
LITERALS = {0x021ECF30: SNOW_SPAWN | 1, 0x021ECF44: SNOW_MOTION | 1}
SNOW_ROWS = (4, 5, 6)                      # weathers that run the snow task
WORK_SIZE, ACC = 0xE0, 0xDC                # snow's 0xDC work + the petal spawn accumulator

# Helpers the petal callbacks call (all verified as snow's own call targets).
ALLOC, REMOVE, GET_POS, SET_POS = 0x021EC1F4, 0x021EC29C, 0x021EC304, 0x021EB5F4
MTRANDOM, SET_FRAME, UDIV = 0x0201FDB8, 0x020249D4, 0x020F2BA4
SNOW_CALLBACK_CALLS = {0x021ECF94: ALLOC, 0x021ECFD8: MTRANDOM, 0x021ECFDE: UDIV, 0x021ECFF6: SET_FRAME,
                       0x021ED012: GET_POS, 0x021ED050: SET_POS, 0x021ED0DE: SET_POS, 0x021ED0E8: REMOVE}
WORK_OFFSET, FIELD_SYSTEM = 0xF58, 0x104

# a/0/6/3 sprite resources
ARCHIVE, STOCK_MEMBERS = 'a/0/6/3', 59
LISTS = {'cell': 55, 'anim': 56, 'char': 57, 'pltt': 58}
LIST_TYPES = {'cell': 2, 'anim': 3, 'char': 0, 'pltt': 1}
SNOW_SET, PETAL_SET, SETS = 1, 10, 10
SNOW_MEMBERS = {'cell': 19, 'anim': 18, 'char': 20, 'pltt': 21}
ENTRY = struct.Struct('<6I')
TERMINATOR = b'\xfe\xff\xff\xff' * 6
RESOURCE_BASE = 1000
PARAMS = struct.Struct('<HHiiHBB')         # map, rate, vx, vy (fx32 px/frame), life, life jitter, spin frames
ASSET = Path(__file__).parent / 'assets' / 'effects' / 'petals.png'
PALETTE_COLORS = 16


def is_transaction(t):
    return isinstance(t, dict) and t.get('schema') == SCHEMA


def areas(state):
    return (state.get('effects') or {}).get('petals', {})


def enabled(state):
    return {int(h): p for h, p in areas(state).items()}


# ---- qualification ------------------------------------------------------------------------------

def _overlay(blob):
    from . import character_runtime as cr
    _, raw = cr.file_by_id(blob, OVERLAY_FILE)
    return bytes(raw)


def _at(data, address, size):
    at = address - OVERLAY_BASE
    return bytes(data[at:at + size])


def _bl_target(source, raw):
    hi, lo = struct.unpack('<HH', raw)
    if hi >> 11 != 0x1E or lo >> 11 not in (0x1F, 0x1D):
        return None
    off = ((hi & 0x7FF) << 12) | ((lo & 0x7FF) << 1)
    if off & (1 << 22):
        off -= 1 << 23
    target = source + 4 + off
    return target & ~3 if lo >> 11 == 0x1D else target


@functools.lru_cache(maxsize=4)
def _qualify(blob):
    data = _overlay(blob)
    for address, stock, _ in HALFWORDS:
        require(struct.unpack('<H', _at(data, address, 2))[0] == stock,
                f'Overlay 1 weather code at {address:#x} differs from the qualified build', 'UNSUPPORTED_RUNTIME')
    require(struct.unpack('<H', _at(data, *DISPATCH_BOUND[:1], 2))[0] == DISPATCH_BOUND[1],
            'Weather dispatcher bound differs', 'UNSUPPORTED_RUNTIME')
    require(struct.unpack('<I', _at(data, TABLE_LITERAL, 4))[0] == STOCK_TABLE, 'Weather table reference differs',
            'UNSUPPORTED_RUNTIME')
    require(_bl_target(LOADER_CALL, _at(data, LOADER_CALL, 4)) == LOADER, 'WeatherManager_New differs',
            'UNSUPPORTED_RUNTIME')
    rows = _at(data, STOCK_TABLE, STOCK_ROWS * ROW)
    for w in SNOW_ROWS:
        sprite, bg, size, *_, task = struct.unpack_from('<HHIIIHHII', rows, w * ROW)
        require((sprite, bg, size, task) == (SNOW_SET, 0xFFFF, 0xDC, SNOW_TASK | 1), 'Snow weather row differs',
                'UNSUPPORTED_RUNTIME')
    for w in range(STOCK_ROWS):
        require(not any(rows[w * ROW + 8:w * ROW + 24]), 'Weather table runtime fields are not clear',
                'UNSUPPORTED_RUNTIME')
    task = _at(data, SNOW_TASK, SNOW_END - SNOW_TASK)
    for site, target in {**SNOW_CALLS, **SNOW_CALLBACK_CALLS}.items():
        require(_bl_target(site, _at(data, site, 4)) == target, f'Snow weather call at {site:#x} differs',
                'UNSUPPORTED_RUNTIME')
    for site in TINT_FLAGS:
        require(_at(data, site, 2) == b'\x28\x5a', 'Snow weather tint flag load differs', 'UNSUPPORTED_RUNTIME')
    for site, value in LITERALS.items():
        require(struct.unpack('<I', _at(data, site, 4))[0] == value, 'Snow weather callback literal differs',
                'UNSUPPORTED_RUNTIME')
    require(member_count(blob, ARCHIVE) == STOCK_MEMBERS, f'{ARCHIVE} member count differs', 'UNSUPPORTED_RUNTIME')
    for kind, member in LISTS.items():
        raw = resource(blob, ARCHIVE, member)[1]
        require(len(raw) == 4 + ENTRY.size * (SETS + 1) and struct.unpack_from('<I', raw)[0] == LIST_TYPES[kind]
                and raw[-ENTRY.size:] == TERMINATOR, f'Weather {kind} resource list differs', 'UNSUPPORTED_RUNTIME')
        for i in range(SETS):
            require(ENTRY.unpack_from(raw, 4 + ENTRY.size * i)[3] == RESOURCE_BASE + i,
                    f'Weather {kind} resource ids differ', 'UNSUPPORTED_RUNTIME')
        require(ENTRY.unpack_from(raw, 4 + ENTRY.size * SNOW_SET)[1] == SNOW_MEMBERS[kind],
                f'Snow {kind} member differs', 'UNSUPPORTED_RUNTIME')
    for kind, magic in (('cell', b'RECN'), ('anim', b'RNAN'), ('char', b'RGCN'), ('pltt', b'RLCN')):
        require(resource(blob, ARCHIVE, SNOW_MEMBERS[kind])[1][:4] == magic, f'Snow {kind} graphics differ',
                'UNSUPPORTED_RUNTIME')
    char = resource(blob, ARCHIVE, SNOW_MEMBERS['char'])[1]
    require(len(char) == 176 and char[16:20] == b'RAHC' and struct.unpack_from('<IIII', char, 28) == (3, 0x10, 0, 128),
            'Snow character tiles differ (4 x 8x8, 4bpp, 1D)', 'UNSUPPORTED_RUNTIME')
    pltt = resource(blob, ARCHIVE, SNOW_MEMBERS['pltt'])[1]
    require(len(pltt) == 552 and pltt[16:20] == b'TTLP' and struct.unpack_from('<I', pltt, 24)[0] == 3,
            'Snow palette differs', 'UNSUPPORTED_RUNTIME')
    return True


def qualify_runtime(blob):
    return _qualify(bytes(blob))


# ---- area parameters ----------------------------------------------------------------------------

def _integer(value, name):
    lo, hi = LIMITS[name]
    require(type(value) is int and lo <= value <= hi, f'Petal {name} is a whole number {lo}..{hi}', 'INVALID_INPUT')
    return value


def derived(p):
    """Runtime numbers for one area's settings: per-frame drift, lifetime, spawn rate, spin."""
    speed = 0.25 + 0.15 * p['speed']                    # px per frame along the drift
    angle = math.radians(p['direction'])                # 0 = straight down, negative = drift left
    vx, vy = speed * math.sin(angle), speed * math.cos(angle)
    life = math.ceil(200 / vy)                          # enters at y -8, ends in the band below y 192
    jitter = min(255, math.ceil(48 / vy))
    target = 4 * p['density']                           # petals on screen at the steady rate
    # Stock spawner at steady state: 6 count units every 3 frames. Petals keep a /1024 accumulator.
    rate = max(1, round(1024 * target / (life + jitter / 2) / 2))
    spin = max(4, 16 - p['speed'])
    return {'vx': round(vx * 4096), 'vy': round(vy * 4096), 'life': life, 'life_jitter': jitter, 'rate': rate,
            'spin_frames': spin, 'on_screen': target, 'speed_px_per_frame': round(speed, 3),
            'seconds_to_cross': round(life / 60, 1)}


def _record(header, p):
    d = derived(p)
    return PARAMS.pack(header, d['rate'], d['vx'], d['vy'], d['life'], d['life_jitter'], d['spin_frames'])


def plan(project, state, index, action, header=None, label=None, **fields):
    require(action in ACTIONS, f"Petal action is one of {', '.join(ACTIONS)}", 'INVALID_INPUT')
    require(type(header) is int, 'Choose a map header', 'INVALID_INPUT')
    require(label is None or isinstance(label, str) and 0 < len(label) <= 160, 'Invalid label', 'INVALID_INPUT')
    require(set(fields) <= set(FIELDS), f"Petal fields are {', '.join(FIELDS)}", 'INVALID_INPUT')
    try:
        head = project.header(header)
    except EditorError as exc:
        raise EditorError('NOT_FOUND', f'No map header {header}') from exc
    qualify_runtime(project.blob)
    current = areas(state)
    before = copy.deepcopy(current.get(str(header)))
    if action == 'remove':
        require(before is not None, f'Header {header} has no petals', 'NOT_FOUND')
        require(not fields, 'Remove takes only the header', 'INVALID_INPUT')
        after, report = None, None
    else:
        merged = {**DEFAULT, **(before or {}), **fields}
        after = {k: _integer(merged[k], k) for k in FIELDS}
        require(after != before, 'The petal settings are unchanged', 'NO_CHANGE')
        require(before is not None or len(current) < MAX_AREAS, f'At most {MAX_AREAS} petal areas', 'RESOURCE_CAPACITY')
        require(head['weather'] not in LIGHTING,
                f"Header {header} uses weather {head['weather']} ({LIGHTING.get(head['weather'])}) for its lighting; "
                'petals would replace it', 'UNSUPPORTED_WEATHER')
        report = {**derived(after), 'replaces_weather': head['weather'], 'name': head['name']}
    return {'schema': SCHEMA, 'index': index, 'action': action, 'header': header,
            'label': label or (f'Petals in {head["name"]}' if action == 'set' else f'Remove petals from {head["name"]}'),
            'request': {'action': action, 'header': header, 'label': label, **copy.deepcopy(fields)},
            'before': before, 'after': after, 'report': report}


def replay(project, state, t, index):
    try:
        expected = plan(project, state, index, **t['request'])
    except (KeyError, TypeError) as exc:
        raise EditorError('INVALID_INPUT', 'Malformed petal effect transaction') from exc
    require(expected == t, 'Petal effect before-value or qualification differs', 'BEFORE_VALUE_MISMATCH')
    table = state.setdefault('effects', {}).setdefault('petals', {})
    if t['after'] is None:
        table.pop(str(t['header']), None)
    else:
        table[str(t['header'])] = copy.deepcopy(t['after'])


def summary(t):
    return {'operation': 'effect.petals', 'index': t['index'], 'action': t['action'], 'header': t['header'],
            'label': t['label'], 'before': t['before'], 'after': t['after'], 'report': t['report']}


def view(project, state):
    rows = []
    for header, p in sorted(enabled(state).items()):
        head = project.header(header)
        rows.append({'header': header, 'name': head['name'], 'weather_replaced': head['weather'], **p,
                     'runtime': derived(p)})
    return {'areas': rows, 'defaults': dict(DEFAULT), 'limits': {k: list(v) for k, v in LIMITS.items()},
            'capacity': {'areas': [len(rows), MAX_AREAS], 'sprites': POOL},
            'weather': WEATHER, 'lighting_refused': sorted(LIGHTING)}


# ---- graphics -----------------------------------------------------------------------------------

@functools.lru_cache(maxsize=1)
def sprite():
    """(4bpp tiles, 16 BGR555 colours) of the four 8x8 petal frames (index 0 transparent)."""
    from PIL import Image
    with Image.open(ASSET) as im:
        require(im.mode == 'P' and im.size == (32, 8), 'Petal sprite must be a 32x8 indexed strip', 'INVALID_ASSET')
        px = list(im.getdata())
        pal = im.getpalette()[:3 * PALETTE_COLORS]
    require(max(px) < PALETTE_COLORS, 'Petal sprite uses more than 16 colours', 'INVALID_ASSET')
    tiles = bytearray()
    for frame in range(4):
        for y in range(8):
            row = px[y * 32 + frame * 8:y * 32 + frame * 8 + 8]
            tiles += bytes(row[i] | row[i + 1] << 4 for i in range(0, 8, 2))
    used = max(px) + 1
    colors = [0] * PALETTE_COLORS
    for i in range(1, used):
        r, g, b = pal[3 * i:3 * i + 3]
        colors[i] = (r >> 3) | (g >> 3) << 5 | (b >> 3) << 10
    return bytes(tiles), tuple(colors)


def graphics(blob):
    """New a/0/6/3 members (character tiles, palette) and the four extended resource lists."""
    tiles, colors = sprite()
    char = bytearray(resource(blob, ARCHIVE, SNOW_MEMBERS['char'])[1])
    char[48:176] = tiles
    pltt = bytearray(resource(blob, ARCHIVE, SNOW_MEMBERS['pltt'])[1])
    struct.pack_into('<16H', pltt, 40, colors[0] or struct.unpack_from('<H', pltt, 40)[0], *colors[1:])
    members = {'char': STOCK_MEMBERS, 'pltt': STOCK_MEMBERS + 1, 'cell': SNOW_MEMBERS['cell'],
               'anim': SNOW_MEMBERS['anim']}
    lists = {}
    for kind, member in LISTS.items():
        raw = resource(blob, ARCHIVE, member)[1]
        entry = list(ENTRY.unpack_from(raw, 4 + ENTRY.size * SNOW_SET))
        entry[1], entry[3] = members[kind], RESOURCE_BASE + PETAL_SET
        lists[member] = bytes(raw[:-ENTRY.size]) + ENTRY.pack(*entry) + TERMINATOR
    return [bytes(char), bytes(pltt)], lists


# ---- runtime ------------------------------------------------------------------------------------

def params_table(state):
    """[u32 count][default record][one record per area] (records: PARAMS)."""
    rows = sorted(enabled(state).items())
    return (struct.pack('<I', len(rows)) + _record(0xFFFF, DEFAULT)
            + b''.join(_record(h, p) for h, p in rows))


def relocated_task(data, address, spawn, motion):
    """Snow's task at ``address`` with the tint off and petal callbacks."""
    from . import character_runtime as cr
    code = bytearray(_at(data, SNOW_TASK, SNOW_END - SNOW_TASK))
    for site, target in SNOW_CALLS.items():
        at = site - SNOW_TASK
        code[at:at + 4] = cr.thumb_bl(address + at, spawn if target == SNOW_SPAWN else target)
    code[TINT_SETUP - SNOW_TASK:TINT_SETUP - SNOW_TASK + 4] = struct.pack('<HH', 0x46C0, 0x46C0)   # no fog tint
    code[TINT_WAIT - SNOW_TASK:TINT_WAIT - SNOW_TASK + 4] = struct.pack('<HH', 0x2001, 0x46C0)     # tint "done"
    for site in TINT_FLAGS:
        code[site - SNOW_TASK:site - SNOW_TASK + 2] = struct.pack('<H', 0x2000)                    # tint flag 0
    struct.pack_into('<I', code, 0x021ECF30 - SNOW_TASK, spawn | 1)
    struct.pack_into('<I', code, 0x021ECF44 - SNOW_TASK, motion | 1)
    return bytes(code)


def lookup_routine(address, table):
    """r0 = weather state -> r0 = the current map's record (the default record when absent)."""
    from .resident import thumb
    return thumb([
        0xB510,                  # push {r4, lr}
        0x6800,                  # ldr r0, [r0]           loader
        ('ldr', 1, FIELD_SYSTEM),
        0x5840,                  # ldr r0, [r0, r1]       fieldSystem
        0x6A00,                  # ldr r0, [r0, #0x20]    location
        0x6800,                  # ldr r0, [r0]           map id
        ('ldr', 1, table),
        0x680A,                  # ldr r2, [r1]           area count
        0x3104,                  # adds r1, #4            default record
        0x1C0C,                  # adds r4, r1, #0
        'loop',
        0x2A00, ('b', 0, 'done'),            # cmp r2, #0; beq done
        0x3110,                  # adds r1, #16
        0x880B,                  # ldrh r3, [r1]
        0x3A01,                  # subs r2, #1
        0x4283, ('b', 1, 'loop'),            # cmp r3, r0; bne loop
        0x1C0C,                  # adds r4, r1, #0
        'done',
        0x1C20,                  # adds r0, r4, #0
        0xBD10,                  # pop {r4, pc}
    ], address)


def spawn_routine(address, lookup):
    """Spawn callback: r0 = weather state, r1 = spawner count units."""
    from .resident import thumb
    return thumb([
        0xB5F8,                  # push {r3-r7, lr}
        0xB084,                  # sub sp, #0x10
        0x1C04,                  # adds r4, r0, #0        state
        0x1C0E,                  # adds r6, r1, #0        count
        ('bl', lookup),
        0x1C05,                  # adds r5, r0, #0        area record
        ('ldr', 0, WORK_OFFSET),
        0x5827,                  # ldr r7, [r4, r0]       weather work
        0x8869,                  # ldrh r1, [r5, #2]      rate
        0x4371,                  # muls r1, r6
        ('ldr', 0, ACC),
        0x583A,                  # ldr r2, [r7, r0]       accumulator
        0x1889,                  # adds r1, r1, r2
        0x0A8E,                  # lsrs r6, r1, #10       petals to spawn
        0x0589, 0x0D89,          # lsls r1, #22; lsrs r1, #22
        0x5039,                  # str r1, [r7, r0]
        0x2E00, ('b', 0, 'done'),            # cmp r6, #0; beq done
        'next',
        0x1C20, 0x2120,          # adds r0, r4, #0; movs r1, #0x20
        ('bl', ALLOC),
        0x2800, ('b', 0, 'done'),            # pool full
        0x9003,                  # str r0, [sp, #0xc]     particle
        0x6887,                  # ldr r7, [r0, #8]       particle work
        0x2000, 0x6038, 0x60F8,  # age = 0; state = 0
        ('bl', MTRANDOM),
        0x2109, ('blx', UDIV),   # r1 = rand % 9
        0x310C,                  # adds r1, #12           speed factor 12..20 (/16)
        0x9102,                  # str r1, [sp, #8]
        0x68A8, 0x4348, 0x1100, 0x60B8,      # vy = (record.vy * f) >> 4
        0x6868, 0x9902, 0x4348, 0x1100, 0x6138,   # vx = (record.vx * f) >> 4
        ('bl', MTRANDOM),
        0x7BA9, 0x3101, ('blx', UDIV),       # r1 = rand % (jitter + 1)
        0x89A8, 0x1840, 0x6078,  # life = record.life + r1
        ('bl', MTRANDOM),
        0x9002,                  # str r0, [sp, #8]
        0x7BE9,                  # ldrb r1, [r5, #15]     spin frames
        0x0A02, 0x2307, 0x401A,  # r2 = (rand >> 8) & 7
        0x1889,                  # adds r1, r1, r2
        0x61F9, 0x6179,          # period = countdown = r1
        0x2103, 0x4001, 0x61B9,  # frame = rand & 3
        0x9803, 0x6840,          # sprite
        ('bl', SET_FRAME),
        ('bl', MTRANDOM),
        ('ldr', 1, 320), ('blx', UDIV),
        0x3940, 0x0309, 0x9100,  # x = (rand % 320 - 64) << 12
        ('ldr', 1, -8 << 12),
        0x9101,                  # y = -8
        0x2100, 0x9102,          # z = 0
        0x9803, 0x6840, 0xA900,  # sprite, &pos
        ('bl', SET_POS),
        0x3E01, ('b', 1, 'next'),            # subs r6, #1; bne next
        'done',
        0xB004,                  # add sp, #0x10
        0xBDF8,                  # pop {r3-r7, pc}
    ], address)


def motion_routine(address):
    """Per-frame particle callback: r0 = particle."""
    from .resident import thumb
    return thumb([
        0xB530,                  # push {r4, r5, lr}
        0xB083,                  # sub sp, #0xc
        0x1C05,                  # adds r5, r0, #0
        0x68AC,                  # ldr r4, [r5, #8]       work
        0x68E0, 0x2800, ('b', 1, 'remove'),  # ended last frame
        0xA800, 0x1C29,          # &pos, particle
        ('bl', GET_POS),
        0x9800, 0x6921, 0x1840, 0x9000,      # x += vx
        0x9801, 0x68A1, 0x1840, 0x9001,      # y += vy
        0x6821, 0x3101, 0x6021,  # age += 1
        0x6860, 0x4281, ('b', 13, 'alive'),  # age <= life
        0x2001, 0x60E0,          # end next frame
        'alive',
        0x6961, 0x3901, 0x6161, ('b', 1, 'move'),   # spin countdown
        0x69E1, 0x6161,          # countdown = period
        0x69A1, 0x3101, 0x2003, 0x4001, 0x61A1,     # frame = (frame + 1) & 3
        0x6868,                  # sprite
        ('bl', SET_FRAME),
        'move',
        0x6868, 0xA900,
        ('bl', SET_POS),
        0xB003, 0xBD30,          # add sp, #0xc; pop {r4, r5, pc}
        'remove',
        0x1C28,
        ('bl', REMOVE),
        0xB003, 0xBD30,
    ], address)


def loader_routine(address, table, row):
    """BL target at LOADER_CALL: run the stock loader, then refresh the 15-row table."""
    from .resident import thumb
    return thumb([
        0xB510,                  # push {r4, lr}
        ('bl', LOADER),
        0x1C04,                  # adds r4, r0, #0        loader
        ('ldr', 0, STOCK_TABLE),
        ('ldr', 1, table),
        0x2200 | (STOCK_ROWS * ROW // 4),    # movs r2, #98
        'stock',
        0xC808, 0xC108, 0x3A01, ('b', 1, 'stock'),  # ldmia r0!, {r3}; stmia r1!, {r3}; subs r2, #1
        ('ldr', 0, row),
        0x2200 | (ROW // 4),     # movs r2, #7
        'petal',
        0xC808, 0xC108, 0x3A01, ('b', 1, 'petal'),
        0x1C20,                  # adds r0, r4, #0
        0xBD10,                  # pop {r4, pc}
    ], address)


def weather_row(task):
    return struct.pack('<HHIIIHHII', PETAL_SET, 0xFFFF, WORK_SIZE, 0, 0, 0, 0, 0, task | 1)


def bindings(project, state, plan, layout):
    """Resident code/tables, overlay 1 patches, header weather and a/0/6/3 resources."""
    if not areas(state):
        return plan
    from . import character_runtime as cr, world
    blob = project.blob
    qualify_runtime(blob)
    raw = _overlay(blob)
    data = bytearray(plan['files'].get(OVERLAY_FILE, raw))
    for address, stock, _ in HALFWORDS:
        require(struct.unpack('<H', _at(data, address, 2))[0] == stock, 'Overlay 1 weather code was already changed',
                'RESOURCE_CONFLICT')
    table = layout.place_boot('petals.weather-table', (STOCK_ROWS + 1) * ROW, 4,
                              note='15 weather descriptors (stock 0..13 refreshed at every field load, petals 14)')
    row = layout.place_boot('petals.weather-row', ROW, 4, note='weather 14 descriptor template')
    params = params_table(state)
    records = layout.place_boot('petals.areas', len(params), 4,
                                note=f'{len(enabled(state))} petal areas + default (map, rate, drift, life, spin)')
    lookup = layout.place_boot('petals.lookup', len(lookup_routine(0, 0)), 4, note='current map -> petal record',
                               kind='code')
    motion = layout.place_boot('petals.motion', len(motion_routine(0)), 4, note='petal drift, spin and end',
                               kind='code')
    spawn = layout.place_boot('petals.spawn', len(spawn_routine(0, 0)), 4, note='petal spawn callback', kind='code')
    task = layout.place_boot('petals.task', SNOW_END - SNOW_TASK, 4,
                             note='snow task ov01_021ECD08 relocated: tint off, petal callbacks', kind='code')
    loader = layout.place_boot('petals.loader', len(loader_routine(0, 0, 0)), 4, kind='code',
                               note=f'BL from WeatherManager_New 0x{LOADER_CALL:08X}: refresh the weather table')
    layout.write(table, _at(raw, STOCK_TABLE, STOCK_ROWS * ROW) + weather_row(task))
    layout.write(row, weather_row(task))
    layout.write(records, params)
    layout.write(lookup, lookup_routine(lookup, records))
    layout.write(motion, motion_routine(motion))
    layout.write(spawn, spawn_routine(spawn, lookup))
    layout.write(task, relocated_task(raw, task, spawn, motion))
    layout.write(loader, loader_routine(loader, table, row))
    for address, _, value in HALFWORDS:
        data[address - OVERLAY_BASE:address - OVERLAY_BASE + 2] = struct.pack('<H', value)
    data[TABLE_LITERAL - OVERLAY_BASE:TABLE_LITERAL - OVERLAY_BASE + 4] = struct.pack('<I', table)
    data[LOADER_CALL - OVERLAY_BASE:LOADER_CALL - OVERLAY_BASE + 4] = cr.thumb_bl(LOADER_CALL, loader)
    result = {**plan, 'files': {**plan['files'], OVERLAY_FILE: bytes(data)},
              'patches': [dict(p) for p in plan['patches']]}
    # Stock headers: weather bits 1..7 of header word 20 in the ARM9 table. Created headers are
    # handled by header_records() before world_runtime packs them.
    arm_start = struct.unpack_from('<I', blob, 0x20)[0]
    stock_count = world.header_count(blob)
    for header in sorted(enabled(state)):
        if header >= stock_count:
            continue
        offset = arm_start + world.HEADER_TABLE + header * world.HEADER_SIZE + 20
        before = bytes(project.arm9[world.HEADER_TABLE + header * world.HEADER_SIZE + 20:
                                    world.HEADER_TABLE + header * world.HEADER_SIZE + 24])
        word = struct.unpack('<I', before)[0]
        after = struct.pack('<I', (word & ~(0x7F << 1)) | WEATHER << 1)
        result['patches'].append({'rom_offset': offset, 'before': before.hex(), 'after': after.hex(),
                                  'kind': 'petals.header-weather'})
    members, lists = graphics(blob)
    require(ARCHIVE not in result.get('appends', {}), f'Another runtime edit appends to {ARCHIVE}', 'RESOURCE_CONFLICT')
    result['appends'] = {**result.get('appends', {}), ARCHIVE: members}
    result['replacements'] = {**result.get('replacements', {}), ARCHIVE: lists}
    result['petals'] = {'areas': len(enabled(state)), 'table': table, 'row': row, 'task': task, 'spawn': spawn,
                        'motion': motion, 'lookup': lookup, 'loader': loader, 'records': records,
                        'members': [STOCK_MEMBERS, STOCK_MEMBERS + 1]}
    return result


def header_raw(state, header, raw):
    """A created header's 24-byte record as exported: weather 14 in a petal area."""
    if header not in enabled(state):
        return raw
    raw = bytearray(raw)
    word = struct.unpack_from('<I', raw, 20)[0]
    struct.pack_into('<I', raw, 20, (word & ~(0x7F << 1)) | WEATHER << 1)
    return bytes(raw)


def header_records(project, state, headers):
    """Created (record, template) pairs with weather 14 in petal areas (world_runtime packs them)."""
    if not areas(state):
        return headers
    created = sorted(project._world_headers)
    require(len(created) == len(headers), 'Created header order differs', 'UNSUPPORTED_RUNTIME')
    return [(header_raw(state, h, raw), template) for h, (raw, template) in zip(created, headers)]
