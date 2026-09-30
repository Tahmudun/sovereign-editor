"""Scripted travel steps for authored events (WORLD-ACCESS-001 and chapter shortcuts).

``warp`` compiles to the single stock HGSS scripted-warp pattern found in all 51 stock
uses in the pinned script archive (a/0/1/2): FadeScreen 6,1,0,black; WaitFade;
Warp map,0,x,z,facing; FadeScreen 6,1,1,black; WaitFade; then the event releases and
ends. Opcodes 174/175/176 (armips scriptmacros.s). Coordinates are global tiles of the
destination header's matrix, as in the save's Location record.

The arrival tile and the follower tile behind the player are qualified against the
composed world at plan time and again by whole-project validation, so a later edit
cannot silently block a guide's destination. Pure bytes; Project owns writes.
"""
from . import world
from .formats import EditorError, require

OPS = {'warp'}
FADE, WAIT_FADE, WARP = 174, 175, 176
# Facing numbering of the Location record / warp command: north, south, west, east.
BEHIND = {0: (0, 1), 1: (0, -1), 2: (1, 0), 3: (-1, 0)}
FACINGS = ('north', 'south', 'west', 'east')
LEDGES = {0x38, 0x39, 0x3A, 0x3B}
SURFABLE = {0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x19, 0x2A, 0x50, 0x51, 0x52, 0x53, 0x73, 0x78, 0x7C}
STEP_LIMIT = 1.25          # runtime |dh| rule (terrain_geometry.BLOCK_DELTA)


def integer(v, lo, hi): return type(v) is int and not isinstance(v, bool) and lo <= v <= hi


def validate(n):
    """Shape checks (project-independent); a warp ends the event, so it has no targets."""
    require(integer(n.get('header'), 0, 65534), 'Choose a destination map header', 'INVALID_EVENT')
    require(integer(n.get('x'), 0, 65535) and integer(n.get('z'), 0, 65535), 'Choose a destination tile', 'INVALID_EVENT')
    require(integer(n.get('facing'), 0, 3), 'Choose the arrival facing (0 north, 1 south, 2 west, 3 east)',
            'INVALID_EVENT')
    return {'id', 'op', 'header', 'x', 'z', 'facing'}, []


def messages(n):
    return []


def compile_node(c, n):
    c.emit('5H', FADE, 6, 1, 0, 0); c.emit('H', WAIT_FADE)
    c.emit('6H', WARP, n['header'], 0, n['x'], n['z'], n['facing'])
    c.emit('5H', FADE, 6, 1, 1, 0); c.emit('H', WAIT_FADE)
    c.jump('$exit')


def _tile(project, state, header, x, z):
    """(pair, height) of one composed tile of ``header``; refusal outside the area."""
    from .border_authoring import cell_of
    from . import terrain_geometry as tg
    from .terrain_authoring import bdhc_bytes
    try:
        cx, cy = cell_of(project, header, x, z)
    except EditorError as exc:
        raise EditorError('INVALID_DESTINATION', f'Tile {x},{z} is outside header {header}') from exc
    ctx = project.context(header=header, cell=[cx, cy])
    raw = project.member_raw(ctx['map_member'])
    offset = world.cell_offset(ctx, x, z)
    pair = bytes(state['permissions'].get((ctx['map_member'], offset), raw[offset:offset + 2]))
    ox, oz = ctx['origin']
    height = tg.height_at(tg.parse_bdhc(bdhc_bytes(project, ctx['map_member'])), x - ox - 16 + 0.5, z - oz - 16 + 0.5)
    return ctx, pair, height


def _occupants(project, state, ctx, x, z):
    from . import event_authoring as ev
    found = []
    for r in ev.records(ev.raw_member(project, ctx['event_member'], state)):
        if r['kind'] == 'npc' and (r['x'], r['z']) == (x, z):
            found.append(f"NPC {r['id']}")
        elif r['kind'] == 'warp' and r['x'] == x and r['z'] in (z, z - 1):
            found.append(f"warp {r['id']}")
        elif r['kind'] == 'trigger' and r['x'] <= x < r['x'] + r['width'] and r['z'] <= z < r['z'] + r['height']:
            found.append(f"trigger {r['id']}")
    return found


def arrival(project, state, header, x, z, facing):
    """Qualify a warp destination: arrival tile plus the follower tile behind it."""
    try:
        project.header(header)
    except EditorError as exc:
        raise EditorError('INVALID_DESTINATION', f'Header {header} does not exist') from exc
    dx, dz = BEHIND[facing]
    rows = []
    for role, (tx, tz) in (('arrival', (x, z)), ('follower', (x + dx, z + dz))):
        ctx, pair, height = _tile(project, state, header, tx, tz)
        problem = ('blocked' if pair[1] & 0x80 or world.is_blocked(pair) else
                   'water' if pair[0] in SURFABLE else 'ledge' if pair[0] in LEDGES else
                   'no floor height' if height is None else None)
        occupied = _occupants(project, state, ctx, tx, tz)
        require(problem is None and not occupied,
                f"Warp {role} tile {tx},{tz} is not clear ({problem or ', '.join(occupied)})", 'INVALID_DESTINATION')
        rows.append({'role': role, 'tile': [tx, tz], 'pair': pair.hex(), 'height': height})
    require(abs(rows[0]['height'] - rows[1]['height']) < STEP_LIMIT,
            'The follower tile is a height step away from the arrival tile', 'INVALID_DESTINATION')
    return {'header': header, 'name': project.header(header)['name'], 'facing': FACINGS[facing], 'tiles': rows}


def qualify(project, state, n):
    return arrival(project, state, n['header'], n['x'], n['z'], n['facing'])
