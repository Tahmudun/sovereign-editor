"""Shops, healing and respawn steps for authored events (SERVICE-01).

Qualified against the pinned ROM and pret/pokeheartgold 9d8b759 (evidence under
evidence/editor-completion-v1/qualification):

* Shops are native special marts. Stock clerks run ``callstd 2011`` (greeting),
  ``holdmsg``, ``setvar 0x8004 N`` and ``callstd 2052`` (buy/sell/quit menu), whose
  ``special_mart_buy`` (ScrCmd_SpecialMartBuy, ARM9) indexes the 30-entry pointer table
  at 0x0210FA3C through its only literal at 0x02048190. In this engine build all 30 stock
  pointers already lead into overlay 131 (the engine keeps its mart lists in the field
  extension). Mart_Init (overlay 3,
  0x02256D34) counts the list up to the 0xFFFF terminator (fewer than 255 entries),
  copies it into its own heap buffer and drops item 4 (Poké Ball) while flag 0x9A is
  unset. The purchase flow is unchanged stock overlay-3 code: funds and bag space are
  checked, the item is added and the money taken once, 10+ Poké Balls add a Premier Ball.
  Authored lists and the relocated table (30 stock pointers, then one per authored shop)
  are appended to overlay 131, which is loaded whenever a field script runs. The list is
  only read while the command runs, so a shop step may not share an event with a battle
  (after a practice loss the battle overlay can occupy that memory).
* Healing inlines std 2069 (script member 3, entry 69) command for command: fade out,
  436 (follower refresh), heal fanfare 1183, heal_party, restore overworld, fade in.
* Respawn selects a stock blackout spawn (sSpawnMaps 0x020F9E80, 30 rows of 18 bytes;
  isBlackoutSpawn bit 8). Blackout heals, warps to that row's death map and runs the
  stock whited-out script (spawn 1 = Mom, others = the Pokémon Center nurse). Authored
  respawn points (travel_points, spawn 31 on) extend the table; a step names them by key.

Pure bytes and validation; Project owns writes.
"""
import struct

from .formats import require, span

OPS = {'shop', 'heal', 'set_spawn'}
CALLSTD, SETVAR, HOLDMSG = 20, 41, 54
MART_INTRO, SPECIAL_MART, MART_VAR = 2011, 2052, 0x8004
FADE, WAIT_FADE, FOLLOWER_REFRESH, FANFARE, WAIT_FANFARE, HEAL, RESTORE, SET_SPAWN = 174, 175, 436, 78, 79, 282, 150, 280
HEAL_FANFARE = 1183
ARM_BASE = 0x02000000
MART_TABLE, MART_LITERAL, STOCK_MARTS = 0x0210FA3C, 0x02048190, 30
MART_MAX_ITEMS, SHOP_MAX_ITEMS, MAX_SHOPS = 254, 48, 32
POKE_BALL, POKE_BALL_FLAG = 4, 0x9A
KEY_POCKET = 7
SPAWN_TABLE, SPAWN_LITERAL, SPAWN_ROWS, SPAWN_SIZE = 0x020F9E80, 0x0203BAE4, 30, 18
SPAWN_NAMES = ('New Bark Town (home)', 'Cherrygrove City', 'Violet City', 'Azalea Town', 'Cianwood City',
               'Goldenrod City', 'Olivine City', 'Ecruteak City', 'Mahogany Town', 'Lake of Rage',
               'Blackthorn City', 'Mt. Silver', 'Pallet Town', 'Viridian City', 'Pewter City', 'Cerulean City',
               'Lavender Town', 'Vermilion City', 'Celadon City', 'Fuchsia City', 'Cinnabar Island',
               'Indigo Plateau', 'Saffron City', 'Safari Zone Gate', 'Battle Frontier', 'Pokéathlon Dome',
               'Route 26 League Gate', 'Route 32', 'Route 3', 'Route 10')


def integer(v, lo, hi): return type(v) is int and not isinstance(v, bool) and lo <= v <= hi


def _arm(blob, address, size):
    start = struct.unpack_from('<I', blob, 0x20)[0]
    return span(blob, start + address - ARM_BASE, size)


def _read(blob, address, size):
    """ARM9 (stored uncompressed in this build) or overlay 131 memory."""
    from . import character_runtime as cr
    field = cr.overlay(blob, 131)
    at = address - field['address']
    if 0 <= at and at + size <= len(field['data']):
        return field['data'][at:at + size]
    require(ARM_BASE <= address and address + size <= ARM_BASE + struct.unpack_from('<I', blob, 0x2C)[0],
            'Mart list outside ARM9 and overlay 131', 'UNSUPPORTED_RUNTIME')
    return _arm(blob, address, size)


# ---- stock tables ------------------------------------------------------------------------------

def spawns(blob):
    """The ROM's spawn rows; only blackout spawns are offered as respawn destinations."""
    from . import world
    require(struct.unpack_from('<I', _arm(blob, SPAWN_LITERAL, 4))[0] == SPAWN_TABLE,
            'Spawn table differs from the qualified build', 'UNSUPPORTED_RUNTIME')
    raw = _arm(blob, SPAWN_TABLE, SPAWN_ROWS * SPAWN_SIZE)
    rows = []
    for i in range(SPAWN_ROWS):
        w = struct.unpack_from('<9H', raw, SPAWN_SIZE * i)
        rows.append({'id': i + 1, 'name': SPAWN_NAMES[i], 'blackout': bool(w[0] >> 8 & 1),
                     'header': w[1], 'map': world.header_name(blob, w[1]), 'x': w[2] & 0xFF, 'z': w[2] >> 8,
                     'script': 'whited out to Mom' if i == 0 else 'whited out to the Pokémon Center'})
    return rows


def stock_marts(blob):
    """The 30 stock special-mart lists (read-only; authored shops never change them)."""
    table = _arm(blob, MART_TABLE, 4 * STOCK_MARTS)
    out = []
    for i in range(STOCK_MARTS):
        address = struct.unpack_from('<I', table, 4 * i)[0]
        items, at = [], address
        while True:
            value = struct.unpack_from('<H', _read(blob, at, 2))[0]
            if value == 0xFFFF:
                break
            items.append(value); at += 2
            require(len(items) < MART_MAX_ITEMS, 'Stock mart list is unterminated', 'UNSUPPORTED_RUNTIME')
        out.append({'index': i, 'address': address, 'items': items})
    return out


def qualify_runtime(blob):
    """Before-values the relocation depends on: the one table literal and the stock lists."""
    require(struct.unpack_from('<I', _arm(blob, MART_LITERAL, 4))[0] == MART_TABLE,
            'Special mart table reference differs from the qualified build', 'UNSUPPORTED_RUNTIME')
    stock_marts(blob)


# ---- shop records ------------------------------------------------------------------------------

def shops(state): return state.get('shops', {})


def shop_index(state):
    """Mart index per shop name: stock marts keep 0..29, authored shops follow in name order."""
    return {name: STOCK_MARTS + i for i, name in enumerate(sorted(shops(state)))}


def item_issue(project, state, item):
    """Why an item cannot be sold in an authored shop ('' when it can)."""
    from . import game_data as gd
    entry = gd.item_entry(project, state, item) if integer(item, 1, 0xFFFE) else None
    if entry is None or not entry['supported']:
        return (entry or {}).get('reason') or 'Unknown item'
    raw = gd._members(project, state, gd.ITEMS)(item)
    word = struct.unpack_from('<H', raw, 8)[0]
    if struct.unpack_from('<H', raw, 0)[0] == 0:
        return 'Has no price (set a price in Records first)'
    if (word >> 7) & 0xF == KEY_POCKET or word >> 5 & 1:
        return 'Key items and HMs cannot be sold'
    return ''


def shop_notes(project, state, items):
    notes = []
    if POKE_BALL in items:
        notes.append('Poké Ball stays hidden by the native mart until the game sets flag 0x9A '
                     '(its own Poké Ball sales flag); authored shops do not change it.')
    if any(i > 536 for i in items):
        notes.append('Expanded items show the engine’s own name, icon and description; native display is untested.')
    return notes


def validate_shop(project, state, name, items):
    from .story_authoring import named
    named(name)
    require(isinstance(items, list) and 1 <= len(items) <= SHOP_MAX_ITEMS,
            f'A shop sells 1..{SHOP_MAX_ITEMS} items', 'INVALID_INPUT')
    require(len(set(items)) == len(items), 'List each item once', 'INVALID_INPUT')
    for item in items:
        issue = item_issue(project, state, item)
        require(not issue, f'Item {item}: {issue}', 'UNSUPPORTED_ID')


def users(state, name):
    from .story_authoring import catalog
    return sorted(k for k, s in catalog(state, 'sequence').items()
                  if any(n.get('op') == 'shop' and n.get('shop') == name for n in s.get('nodes', [])))


# ---- steps -------------------------------------------------------------------------------------

def validate(n):
    """Shape checks (project-independent); returns (fields, targets)."""
    op = n['op']
    if op == 'shop':
        require(isinstance(n.get('shop'), str) and n['shop'], 'Choose a shop inventory', 'INVALID_EVENT')
        return {'id', 'op', 'shop'}, ['next']
    if op == 'set_spawn':
        # A stock blackout spawn (1..30) or an authored respawn point (travel_points key).
        if 'point' in n:
            require(isinstance(n['point'], str) and n['point'], 'Choose a respawn point', 'INVALID_EVENT')
            return {'id', 'op', 'point'}, ['next']
        require(integer(n.get('spawn'), 1, SPAWN_ROWS), 'Choose a respawn point', 'INVALID_EVENT')
        return {'id', 'op', 'spawn'}, ['next']
    return {'id', 'op'}, ['next']


def qualify(project, state, n):
    if n['op'] == 'shop':
        require(n['shop'] in shops(state), f"Unknown shop {n['shop']}: define its inventory in Records first",
                'NOT_FOUND')
        qualify_runtime(project.blob)
    elif n['op'] == 'set_spawn' and 'point' in n:
        from . import travel_points
        travel_points.spawn_id(state, n['point'])
    elif n['op'] == 'set_spawn':
        row = spawns(project.blob)[n['spawn'] - 1]
        require(row['blackout'], f"{row['name']} is not a blackout spawn in this ROM", 'UNSUPPORTED_ID')


def check_event(nodes):
    """A shop's list lives in field-only memory: keep battles out of its event."""
    if any(n['op'] == 'shop' for n in nodes):
        require(not any(n['op'] == 'battle' for n in nodes),
                'A shop needs its own clerk event (no battle in the same event)', 'INVALID_EVENT')


def compile_node(c, n, env):
    op = n['op']
    if op == 'shop':
        index = (env.get('shops') or {}).get(n['shop'])
        require(index is not None, f"Unknown shop {n['shop']}", 'NOT_FOUND')
        c.emit('2H', CALLSTD, MART_INTRO); c.emit('H', HOLDMSG)
        c.emit('3H', SETVAR, MART_VAR, index); c.emit('2H', CALLSTD, SPECIAL_MART)
    elif op == 'heal':
        c.emit('5H', FADE, 6, 1, 0, 0); c.emit('H', WAIT_FADE); c.emit('H', FOLLOWER_REFRESH)
        c.emit('2H', FANFARE, HEAL_FANFARE); c.emit('H', WAIT_FANFARE); c.emit('H', HEAL); c.emit('H', RESTORE)
        c.emit('5H', FADE, 6, 1, 1, 0); c.emit('H', WAIT_FADE)
    elif 'point' in n:
        spawn = (env.get('travel') or {}).get(n['point'])
        require(spawn is not None, f"Unknown respawn point {n['point']}", 'NOT_FOUND')
        c.emit('2H', SET_SPAWN, spawn)
    else:
        c.emit('2H', SET_SPAWN, n['spawn'])
    c.jump(n['next'])


# ---- runtime -----------------------------------------------------------------------------------

def bindings(blob, plan, state):
    """Append authored lists and the relocated table to overlay 131; repoint the literal."""
    if not shops(state):
        return plan
    from . import character_runtime as cr
    qualify_runtime(blob)
    require(len(shops(state)) <= MAX_SHOPS, f'At most {MAX_SHOPS} authored shops', 'RESOURCE_CAPACITY')
    field = cr.overlay(blob, 131)
    data = bytearray(plan['files'].get(field['file_id'], field['data']))
    data.extend(b'\0' * (-len(data) % 4))
    pointers = [struct.unpack_from('<I', _arm(blob, MART_TABLE, 4 * STOCK_MARTS), 4 * i)[0] for i in range(STOCK_MARTS)]
    rows = []
    for name in sorted(shops(state)):
        items = shops(state)[name]
        address = field['address'] + len(data)
        data.extend(struct.pack(f'<{len(items)}H', *items) + b'\xff\xff')
        data.extend(b'\0' * (-len(data) % 4))
        pointers.append(address)
        rows.append({'name': name, 'index': STOCK_MARTS + len(rows), 'address': address, 'items': len(items)})
    table = field['address'] + len(data)
    data.extend(struct.pack(f'<{len(pointers)}I', *pointers))
    require(len(data) <= cr.FIELD_LIMIT, 'Shop lists exceed the field extension reservation', 'RESOURCE_CAPACITY')
    plan['files'][field['file_id']] = bytes(data)
    start = struct.unpack_from('<I', blob, 0x20)[0]
    plan['patches'].append({'rom_offset': start + MART_LITERAL - ARM_BASE, 'before': struct.pack('<I', MART_TABLE).hex(),
                            'after': struct.pack('<I', table).hex(), 'kind': 'shop.mart-table'})
    offset = field['table_offset'] + 8
    existing = next((p for p in plan['patches'] if p['rom_offset'] == offset), None)
    if existing:
        existing['after'] = struct.pack('<I', len(data)).hex()
    else:
        plan['patches'].append({'rom_offset': offset, 'before': span(blob, offset, 4).hex(),
                                'after': struct.pack('<I', len(data)).hex(), 'kind': 'shop.overlay-size'})
    plan['shops'] = {'table': table, 'rows': rows}
    return plan
