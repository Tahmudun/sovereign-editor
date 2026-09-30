"""Native field-move features on authored terrain (FIELD-02 Whirlpool; more families follow).

Qualified against the pinned ROM and pret/pokeheartgold 9d8b759 (src/field_move.c,
asm/overlay_01_021F1AFC.s; ledger work/original-content-v1/impl/LEDGER.md):

* Whirlpool: FieldMove_InitCheckData sets the whirlpool flag when the FACING tile has
  behavior 0x11. While surfing, A at it runs std 10017 (member 146 entry 16: party move
  Whirlpool 250 + Glacier badge, "use Whirlpool?"), whose Whirlpool command starts
  CallFieldTask_Whirlpool (overlay 1 0x021F2DA4): forced steps (movement 4) in the facing
  direction while the tile under the player is still a whirlpool, stopping on the first
  other tile. Every stock whirlpool (Route 27 members 14/15, Route 41 members 64..67) is a
  3x3 block of ``11 80`` (behavior 0x11, blocked) on sea ``15 00`` with building model 43
  ``uzushio`` at the block centre, 0.5 tile up (the sea plate), unrotated, scale 1.
  An authored whirlpool is exactly that on authored or stock surfable water: nine
  permission pairs and one stock placement record. Crossing is symmetric (use it again
  from the far side); every row/column whose approach tile is surfable must land on
  surfable water on the other side.

Transactions (``sovereign-field-feature-transaction-v1``) place or remove one feature by
a stable key in one map cell. A feature owns its tiles and its placement: other editors
see a blocked water tile and a placed object; remove restores the recorded pairs.
Pure planning; Project owns writes.
"""
import copy
import math
import re
import struct

from . import authoring, world
from .formats import EditorError, require

SCHEMA = 'sovereign-field-feature-transaction-v1'
ACTIONS = ('place', 'remove')
FAMILIES = ('whirlpool',)
KEY = re.compile(r'[a-z][a-z0-9_]{0,23}')
WHIRLPOOL = {'behavior': 0x11, 'pair': bytes((0x11, 0x80)), 'model': 43, 'texture': 'uzushio', 'size': 3,
             'move': 250, 'badge': 6, 'script': 10017, 'donor': (14, 0)}
SURF_WATER = {0x10, 0x15}          # river, sea: the stock whirlpools sit on sea (0x15)


def is_transaction(t):
    return isinstance(t, dict) and t.get('schema') == SCHEMA


def features(state):
    return state.get('field_features') or {}


def _pair(project, state, ctx, x, z):
    member = ctx['map_member']
    offset = world.cell_offset(ctx, x, z)
    return offset, bytes(state['permissions'].get((member, offset), project.member_raw(member)[offset:offset + 2]))


def _surfable(pair):
    return pair[0] in SURF_WATER and not pair[1] & 0x80


def _textures(project, ctx, state):
    from . import mapscene, nitro
    refs, blobs = mapscene.tilesets(project, ctx, state)
    require(refs['building_models']['archive'] == 'a/0/4/0', 'Whirlpools are outdoor features', 'UNSUPPORTED_CONTEXT')
    names, _ = nitro.texture_set(blobs['building_tileset'])
    return set(names)


def _plan_whirlpool(project, ctx, state, x, z, before_tiles=None):
    from . import travel
    size = WHIRLPOOL['size']
    ox, oz = ctx['origin']
    require(type(x) is int and type(z) is int and ox <= x and x + size <= ox + world.MAP_SIZE
            and oz <= z and z + size <= oz + world.MAP_SIZE,
            f'A whirlpool is {size}x{size} tiles inside one map cell (x, z = its north-west tile)', 'OUTSIDE_MAP')
    require(WHIRLPOOL['texture'] in _textures(project, ctx, state),
            'This area’s building textures have no whirlpool (stock areas 2 and 7 do)', 'UNSUPPORTED_CONTEXT')
    block = [(x + i, z + j) for j in range(size) for i in range(size)]
    from . import terrain_authoring
    tiles = []
    for tx, tz in block:
        offset, pair = _pair(project, state, ctx, tx, tz)
        require(_surfable(pair), f'Tile {tx},{tz} is not open Surf water ({pair.hex()})', 'UNSUPPORTED_TERRAIN')
        occupied = travel._occupants(project, state, ctx, tx, tz)
        require(not occupied, f"Tile {tx},{tz} holds {', '.join(occupied)}", 'EVENT_CONFLICT')
        tiles.append({'x': tx, 'z': tz, 'offset': offset, 'before': pair.hex(), 'after': WHIRLPOOL['pair'].hex()})
    crossings = []
    for axis in ('row', 'column'):
        for k in range(size):
            if axis == 'row':
                ends = [(x - 1, z + k), (x + size, z + k)]
            else:
                ends = [(x + k, z - 1), (x + k, z + size)]
            state_ = []
            for ex, ez in ends:
                if not (ox <= ex < ox + world.MAP_SIZE and oz <= ez < oz + world.MAP_SIZE):
                    state_.append(False)
                    continue
                _, pair = _pair(project, state, ctx, ex, ez)
                state_.append(_surfable(pair) and not travel._occupants(project, state, ctx, ex, ez))
            require(state_[0] == state_[1], f'The whirlpool {axis} {k} can be entered from one side only: its far '
                    'side must be open water too (or blocked on both sides)', 'UNSUPPORTED_ACCESS')
            if state_[0]:
                crossings.append({'axis': axis, 'index': k, 'ends': [list(e) for e in ends]})
    require(crossings, 'Nothing can reach the whirlpool: open water must touch it on two opposite sides',
            'UNSUPPORTED_ACCESS')
    centre = {'x': x + size / 2, 'z': z + size / 2}
    from . import scenery
    height = scenery.floor_height(project, ctx, centre)
    words = authoring.record_from_global(ctx, centre, {'y': world.record_value(height)})
    return tiles, crossings, words, height


def plan(project, context, state, index, action, key=None, family=None, x=None, z=None, label=None):
    require(action in ACTIONS, f"Field feature action is one of {', '.join(ACTIONS)}", 'INVALID_INPUT')
    require(isinstance(key, str) and KEY.fullmatch(key), 'A field feature needs a key (lowercase, <= 24)',
            'INVALID_INPUT')
    require(label is None or isinstance(label, str) and 0 < len(label) <= 160, 'Invalid label', 'INVALID_INPUT')
    current = features(state)
    before = copy.deepcopy(current.get(key))
    member = context['map_member']
    from . import scenery
    table = scenery.table_for(project, context, state)
    if action == 'place':
        require(before is None, f'Field feature {key} already exists', 'EXISTS')
        require(family in FAMILIES, f"Family is one of {', '.join(FAMILIES)}", 'INVALID_INPUT')
        tiles, crossings, words, height = _plan_whirlpool(project, context, state, x, z)
        require(len(table) < 32, 'This map already has 32 placed objects (runtime limit)', 'RESOURCE_CAPACITY')
        slot = len(project.member_data(member)[1]) + index
        require(slot not in table, 'Created object slot already exists', 'STALE_EDIT')
        # The stock Route 27 whirlpool record (member 14 slot 0), moved: every other word is stock.
        template = project.member_data(WHIRLPOOL['donor'][0])[1][WHIRLPOOL['donor'][1]]
        require(template['model_id'] == WHIRLPOOL['model'], 'The stock whirlpool record differs', 'BEFORE_VALUE_MISMATCH')
        offset = template['record_offset']
        raw = bytearray(project.member_raw(WHIRLPOOL['donor'][0])[offset:offset + 48])
        struct.pack_into('<3i', raw, 4, words['x'], words['y'], words['z'])
        after = {'family': family, 'context': authoring.context_ref(context), 'member': member, 'x': x, 'z': z,
                 'slot': slot, 'record': bytes(raw).hex(), 'height': height, 'tiles': tiles,
                 'crossings': crossings}
    else:
        require(before is not None, f'No field feature {key}', 'NOT_FOUND')
        require(before['context'] == authoring.context_ref(context), 'Choose the feature’s map cell', 'CONTEXT_MISMATCH')
        require(x is None and z is None and family is None, 'Remove takes only the key', 'INVALID_INPUT')
        for t in before['tiles']:
            _, pair = _pair(project, state, context, t['x'], t['z'])
            require(pair.hex() == t['after'], f"Tile {t['x']},{t['z']} was changed after the feature was placed",
                    'STALE_EDIT')
        after = None
    return {'schema': SCHEMA, 'index': index, 'action': action, 'key': key, 'context': authoring.context_ref(context),
            'label': label or f'{(family or before["family"]).capitalize()} {action}: {key}',
            'request': {'action': action, 'key': key, 'family': family, 'x': x, 'z': z, 'label': label},
            'before': before, 'after': after}


def apply(project, state, t, context):
    """Write a planned transaction into the composed state (objects, placements, permissions)."""
    from . import scenery
    member = context['map_member']
    table = scenery.table_for(project, context, state)
    if t['after'] is None:
        spec = t['before']
        del table[spec['slot']]
        state['placements'].pop((member, spec['slot']), None)
        for tile in spec['tiles']:
            pair = bytes.fromhex(tile['before'])
            state['permissions'][(member, tile['offset'])] = pair
            state['generic']['permissions'][(member, tile['offset'])] = pair
        del state['field_features'][t['key']]
    else:
        spec = t['after']
        raw = bytes.fromhex(spec['record'])
        table[spec['slot']] = {'id': f"feature:{t['key']}", 'raw': raw}
        state['placements'][(member, spec['slot'])] = dict(zip(('x', 'y', 'z'), struct.unpack_from('<3i', raw, 4)))
        for tile in spec['tiles']:
            pair = bytes.fromhex(tile['after'])
            state['permissions'][(member, tile['offset'])] = pair
            state['generic']['permissions'][(member, tile['offset'])] = pair
        state.setdefault('field_features', {})[t['key']] = copy.deepcopy(spec)
    state['structural_members'].add(member)
    state['contexts'].append(context)


def replay(project, state, t, index):
    try:
        ctx = project.context(header=t['context']['header'], cell=t['context']['cell'])
        expected = plan(project, ctx, state, index, **t['request'])
    except (KeyError, TypeError) as exc:
        raise EditorError('INVALID_INPUT', 'Malformed field feature transaction') from exc
    require(expected == t, 'Field feature before-value or qualification differs', 'BEFORE_VALUE_MISMATCH')
    apply(project, state, t, ctx)


def summary(t):
    spec = t['after'] or t['before']
    return {'operation': 'field.transaction', 'index': t['index'], 'action': t['action'], 'key': t['key'],
            'family': spec['family'], 'context': t['context'], 'label': t['label'],
            'tile': [spec['x'], spec['z']], 'crossings': len(spec['crossings'])}


def view(state):
    return {'features': [{'key': k, **{f: v[f] for f in ('family', 'context', 'x', 'z', 'slot', 'crossings')}}
                         for k, v in sorted(features(state).items())],
            'families': {'whirlpool': 'Surf water 3x3: a party Pokémon with Whirlpool and the Glacier Badge '
                                      'crosses it (stock std 10017)'}}
