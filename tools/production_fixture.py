"""Production authoring v2 fixture: qualification load + Canopy Walk / Ranger Room.

Technical fixture, not story content. Every entry is an ordinary `area-edit`
operation (the same Project operations the editors use); this module holds no
bytes and applies nothing. Stages are applied in order, each as ONE atomic batch
(<= 64 operations). IDs are derived by Project allocation; the comments state the
values expected on a clone of r43 (headers 542.., trainer IDs 743.., etc.).

Qualification load (working targets, including r43's inherited content):
  32 characters (31 new; one reviewed package reused under distinct identities),
  128 named states (60 number + 68 on/off), 64 trainers (40 new ordinary, 19 practice),
  128 created headers (Canopy Walk 4x4, Ranger Room, 124 one-cell load rooms),
  32 persistent-visibility actors (22 new), 144 created cells in aggregate.

Usage: production_fixture.py PROJECT STAGE OUT.json    (writes {"operations": [...], "label": ...})
"""
import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PACKAGE = ROOT / 'tests/fixtures/tiana.character.json'
R43_STATES, R43_TRAINERS, R43_CHARACTERS, R43_HIDDEN = 11, 5, 1, 10
NEW_CHARACTERS = 31
NUMBER_STATES = 60 - R43_STATES                 # 49 new number states (legacy 5 + 44 extra variables)
ORDINARY = 40                                   # new ordinary trainers, each with its own on/off defeat state
PRACTICE = 64 - R43_TRAINERS - ORDINARY         # 19
OTHER_SWITCHES = 128 - 60 - ORDINARY - 1        # 27 plain on/off states (+ canopy_lamp)
LOAD_ROOMS = 124
PRESENCE_ROOMS = 21                             # load rooms with a stage-owned (presence) actor
WINDOW = (39, 11)                               # stock matrix 0 window for Canopy Walk
LIBRARY = {'header': 33, 'cell': [18, 12]}      # any accepted context for library operations


def package(i):
    value = json.loads(PACKAGE.read_text())
    value['name'] = f'Ranger {i:02d}'
    value['gender'] = 'female' if i % 2 else 'male'
    return value


def story(kind, key, value, context=LIBRARY):
    return {'kind': 'story', 'context': context, 'request': {'kind': kind, 'key': key, 'value': value}}


def characters():
    return [story('character', f'ranger_{i:02d}', package(i)) for i in range(1, NEW_CHARACTERS + 1)]


def states():
    ops = [story('state', f'load_number_{i:02d}', {'name': f'Load number {i:02d}'}) for i in range(1, NUMBER_STATES)]
    ops.append(story('state', 'canopy_stage', {'name': 'Canopy Walk stage'}))     # the 60th number state
    ops += [story('state', f'ranger_{i:02d}_defeated', {'name': f'Ranger {i:02d} defeated', 'switch': True})
            for i in range(1, ORDINARY)]
    ops.append(story('state', 'canopy_trainer_defeated', {'name': 'Canopy Walk trainer defeated', 'switch': True}))
    ops.append(story('state', 'canopy_lamp', {'name': 'Canopy lamp lit', 'switch': True}))
    ops += [story('state', f'load_switch_{i:02d}', {'name': f'Load switch {i:02d}', 'switch': True})
            for i in range(1, OTHER_SWITCHES + 1)]
    return ops


PARTY = [{'species': 16, 'level': 6, 'moves': None, 'held_item': 0}, {'species': 19, 'level': 6, 'moves': None, 'held_item': 0}]


def trainers():
    ops = []
    for i in range(1, PRACTICE + 1):
        ops.append(story('trainer', f'practice_{i:02d}', {
            'name': f'Trainee {i:02d}', 'character': f'ranger_{1 + i % NEW_CHARACTERS:02d}', 'stock_class': None,
            'party': [{'species': 161, 'level': 4}], 'before': ['Practice battle?'], 'after': ['Thanks!']}))
    for i in range(1, ORDINARY):
        ops.append(story('trainer', f'ranger_{i:02d}', {
            'name': f'Ranger {i:02d}', 'character': f'ranger_{i:02d}' if i <= NEW_CHARACTERS else None,
            'stock_class': None if i <= NEW_CHARACTERS else 4, 'policy': 'ordinary-single-v1',
            'defeat_state': f'ranger_{i:02d}_defeated', 'party': copy.deepcopy(PARTY),
            'before': ['Ranger training!'], 'after': ['Well fought.'], 'revisit': ['Good luck out there.']}))
    # Slot 63: the rehearsal trainer, drawn with the highest character slot (31).
    ops.append(story('trainer', 'canopy_trainer', {
        'name': 'Ranger Ivy', 'character': f'ranger_{NEW_CHARACTERS:02d}', 'stock_class': None,
        'policy': 'ordinary-single-v1', 'defeat_state': 'canopy_trainer_defeated',
        'party': [{'species': 16, 'level': 7, 'moves': [33, 28, 16, 0], 'held_item': 0},
                  {'species': 161, 'level': 7, 'moves': [33, 98, 111, 0], 'held_item': 155}],
        'before': ['The canopy path is', 'my patrol. Battle me!'], 'after': ['You walk this path well.'],
        'revisit': ['The canopy is calm now.']}))
    return ops


def window_cells(project):
    grid = project.matrix_data(0)
    return [{'cell': [x, y], 'source': {'header': grid['headers'][WINDOW[1] + y][WINDOW[0] + x],
                                        'cell': [WINDOW[0] + x, WINDOW[1] + y]}} for y in range(4) for x in range(4)]


def areas(project):
    """Canopy Walk first, then the load rooms, then Ranger Room: the rehearsal's room is
    the last (highest) created header, so reaching it is the high-header travel test."""
    cells = window_cells(project)
    ops = [{'kind': 'world', 'context': cells[0]['source'], 'request': {
        'action': 'create', 'identity': 'canopy_walk', 'name': 'Canopy Walk', 'internal_name': 'CANOPY_WALK',
        'template_header': 23, 'encounters': 'template', 'worldmap': None, 'close': False, 'cells': cells,
        'label': 'Create Canopy Walk (4x4 stock Route 15/14 window with its forest, sea and altitudes)'}}]
    for i in range(LOAD_ROOMS):
        ops.append({'kind': 'world', 'context': {'header': 72, 'cell': [0, 0]}, 'request': {
            'action': 'create', 'identity': f'load_room_{i:03d}', 'name': f'Load Room {i:03d}',
            'internal_name': f'LOAD_ROOM_{i:03d}', 'template_header': 72, 'encounters': 'none', 'worldmap': None,
            'close': True, 'cells': [{'cell': [0, 0], 'source': {'header': 72, 'cell': [0, 0]}}]}})
    ops.append({'kind': 'world', 'context': {'header': 72, 'cell': [0, 0]}, 'request': {
        'action': 'create', 'identity': 'ranger_room', 'name': 'Ranger Room', 'internal_name': 'RANGER_ROOM',
        'template_header': 72, 'encounters': 'none', 'worldmap': None, 'close': True,
        'cells': [{'cell': [0, 0], 'source': {'header': 72, 'cell': [0, 0]}}], 'label': 'Create Ranger Room'}})
    return ops


def header_of(project, identity):
    return next(a['header'] for a in project.world_areas()['areas'] if a['identity'] == identity)


# ---- rehearsal layout (global tiles of the created 4x4 area; see the beat sheet) ----------
# Route 15 maps 166|167 are cells [1,2]|[2,2] (seam x=63|64). Bordered path and grass
# windows are the reviewed PROD-VIS-001 reroute: an S path through the upper road band and
# new tall grass in a cleared stretch of the lower band, both crossing the seam.
TOP_WINDOW = {'x': 42, 'z': 69, 'width': 27, 'height': 5}
NEW_PATH = ([{'x': x, 'z': 70} for x in range(42, 51)] + [{'x': 50, 'z': 71}]
            + [{'x': x, 'z': 72} for x in range(50, 69)])
BOTTOM_WINDOW = {'x': 48, 'z': 81, 'width': 36, 'height': 4}
NEW_GRASS = [{'x': x, 'z': z} for z in (82, 83) for x in range(58, 70)]
# Collision-only closures where the stock route leaves the enclosed view: in front of the
# west-jump ledge column x=40 (the lane beyond runs to the area's west edge) and in the
# Route 13 fence gap north of the Route 14 road (its lanes run to the east edge).
WEST_CLOSURE = [{'x': 41, 'z': z} for z in range(68, 74)]
NORTH_CLOSURE = [{'x': x, 'z': 23} for x in range(107, 110)]
# Compatible building: the Route 12 house (model 41, door 24; same Kanto building set and
# floor height 1 as Canopy Walk). Stock 4x5 footprint at R12 1428..1431 x 314..318, door
# tile at its column 1 of the bottom row; model = footprint (left+2, top+3.5).
KANTO_HOUSE = {'header': 20, 'cell': [44, 9], 'x': 1428, 'z': 314, 'size': (4, 5), 'door': (1, 4),
               'slots': (0, 1), 'model': (2.0, 3.5), 'door_model': (2.1129913330078125, 4.262741088867188)}
# Johto house (Cherrygrove model 37, door 50; floor height 1 like Route 29). Stock 5x5
# footprint at 546..550 x 395..399, door at its column 1 of the bottom row.
JOHTO_HOUSE = {'header': 67, 'cell': [17, 12], 'x': 546, 'z': 395, 'size': (5, 5), 'door': (1, 4),
               'slots': (6, 7), 'model': (2.0, 3.5), 'door_model': (2.12451171875, 4.2314605712890625)}
HUT = (92, 76)            # Canopy Walk arrival hut footprint (top-left), junction cell [2,2]
LODGE_HOUSE = (76, 80)    # Ranger Room house footprint, lower lane cell [2,2]
ROUTE29_LODGE = (632, 402)  # new Route 29 lodge, meadow pocket east of the ledge in cell 19,12
ROUTE29 = {'header': 33, 'cell': [19, 12]}
ROOM_MAT = (4, 8)         # exit mat of the copied room (h72 donor)


def building(kind, destination, left_top, label):
    """Import a stock building + its door model and stamp its stock footprint (with door)."""
    (bx, bz), (mx, mz), (dx, dz) = left_top, kind['model'], kind['door_model']
    donor = {'header': kind['header'], 'cell': kind['cell']}
    w, h = kind['size']
    return [
        {'kind': 'scenery', 'context': donor, 'request': {
            'operation': 'import', 'slot': kind['slots'][0], 'x': bx + mx, 'z': bz + mz,
            'destination': dict(destination), 'label': f'{label}: building'}},
        {'kind': 'scenery', 'context': donor, 'request': {
            'operation': 'import', 'slot': kind['slots'][1], 'x': bx + dx, 'z': bz + dz,
            'destination': dict(destination), 'label': f'{label}: door'}},
        {'kind': 'terrain', 'context': dict(destination), 'request': {
            'x': bx, 'z': bz, 'width': w, 'height': h, 'label': f'{label}: stock footprint and door',
            'copy_from': {'header': kind['header'], 'cell': kind['cell'], 'x': kind['x'], 'z': kind['z']}}}]


def door(kind, left_top):
    return left_top[0] + kind['door'][0], left_top[1] + kind['door'][1]


def layout(project):
    cw, room = header_of(project, 'canopy_walk'), header_of(project, 'ranger_room')
    west, east, north = {'header': cw, 'cell': [1, 2]}, {'header': cw, 'cell': [2, 2]}, {'header': cw, 'cell': [3, 0]}
    hx, hz = door(KANTO_HOUSE, HUT)
    lx, lz = door(KANTO_HOUSE, LODGE_HOUSE)
    rx, rz = door(JOHTO_HOUSE, ROUTE29_LODGE)
    return [
        {'kind': 'border', 'context': west, 'request': {'family': 'path', 'tiles': NEW_PATH, 'window': TOP_WINDOW,
                                                        'label': 'Reroute the upper road: S path across the seam'}},
        {'kind': 'border', 'context': west, 'request': {'family': 'path', 'tiles': [], 'window': BOTTOM_WINDOW,
                                                        'label': 'Clear the lower road stretch to meadow'}},
        {'kind': 'border', 'context': west, 'request': {'family': 'tall_grass', 'tiles': NEW_GRASS,
                                                        'label': 'New bordered tall grass across the seam'}},
        {'kind': 'terrain', 'context': west, 'request': {'tiles': WEST_CLOSURE, 'blocked': True,
                                                         'label': 'Close the west lane before the ledge'}},
        {'kind': 'terrain', 'context': north, 'request': {'tiles': NORTH_CLOSURE, 'blocked': True,
                                                          'label': 'Close the Route 13 fence gap'}},
        *building(KANTO_HOUSE, east, HUT, 'Canopy Walk trail hut'),
        *building(KANTO_HOUSE, east, LODGE_HOUSE, 'Ranger Room house'),
        *building(JOHTO_HOUSE, ROUTE29, ROUTE29_LODGE, 'Route 29 Canopy Walk lodge'),
        {'kind': 'world', 'context': dict(ROUTE29), 'request': {
            'action': 'connect', 'x': rx, 'z': rz, 'destination': {'header': cw}, 'arrival': {'x': hx, 'z': hz},
            'label': 'Route 29 lodge <-> Canopy Walk trail hut'}},
        {'kind': 'world', 'context': dict(east), 'request': {
            'action': 'connect', 'x': lx, 'z': lz, 'destination': {'header': room},
            'arrival': {'x': ROOM_MAT[0], 'z': ROOM_MAT[1]}, 'label': 'Ranger Room house <-> Ranger Room'}}]


CAMPER, SCIENTIST, KEEPER = 345, 338, 329        # stock human appearances reviewed in r43
NPC = dict(kind='npc', donor_id=None, movement=0, range_x=0, range_z=0, character=None, once_state=None)
SCOUT = (89, 80)          # grass west of the trail hut, facing south to the road
TRAINER = (58, 70)        # upper lane meadow north of the new path, facing south
LAMP = (80, 82)           # lower-lane meadow beside the Ranger Room house, facing south
ENCOUNTER_SPECIES = [161, 16, 19, 161, 16, 19, 161, 16, 19, 16, 19, 161]
ENCOUNTER_LEVELS = [3, 3, 4, 4, 4, 5, 5, 3, 4, 5, 5, 4]


def room_tile(project, header):
    """First free flat tile of a copied room at least three steps from its exit mat."""
    from sovereign_editor import scenery, world
    from sovereign_editor.formats import EditorError
    ctx = project.context(header=header, cell=[0, 0])
    raw = project.member_raw(ctx['map_member'])
    for z in range(2, 12):
        for x in range(1, 10):
            at = world.cell_offset(ctx, x, z)
            if world.is_blocked(raw[at:at + 2]) or raw[at] != 0 or abs(x - ROOM_MAT[0]) + abs(z - ROOM_MAT[1]) < 3:
                continue
            try:
                scenery.floor_height(project, ctx, {'x': x + .5, 'z': z + .5})
            except EditorError:
                continue
            return x, z
    raise EditorError('NOT_FOUND', 'No free room tile')


def sequence(context, key, value):
    return {'kind': 'story', 'context': context, 'request': {'kind': 'sequence', 'key': key, 'value': value}}


def presence(project):
    """21 load rooms each get a stage-owned actor shown while its load switch is off, so the
    rehearsal lamp (allocated after them) holds the 32nd and highest visibility flag."""
    ops = []
    for i in range(PRESENCE_ROOMS):
        header = header_of(project, f'load_room_{i:03d}')
        x, z = room_tile(project, header)
        ops.append(sequence({'header': header, 'cell': [0, 0]}, f'load_presence_{i:02d}', {
            **NPC, 'x': x, 'z': z, 'facing': 1, 'stock_sprite': KEEPER,
            'presence': {'state': f'load_switch_{i + 1:02d}', 'values': [0]},
            'nodes': [{'id': 'hi', 'op': 'say', 'pages': [f'Load room {i:03d}.'], 'next': 'done'},
                      {'id': 'done', 'op': 'end'}]}))
    return ops


def rehearsal_story(project):
    cw, room = header_of(project, 'canopy_walk'), header_of(project, 'ranger_room')

    def at(tile):
        return {'header': cw, 'cell': [tile[0] // 32, tile[1] // 32]}
    kx, kz = room_tile(project, room)
    return [
        sequence(at(SCOUT), 'canopy_scout', {**NPC, 'x': SCOUT[0], 'z': SCOUT[1], 'facing': 1, 'stock_sprite': SCIENTIST, 'nodes': [
            {'id': 'stage', 'op': 'if', 'state': 'canopy_stage', 'value': 0, 'yes': 'welcome', 'no': 'again'},
            {'id': 'welcome', 'op': 'say', 'pages': ['Welcome to Canopy Walk!', 'The Ranger Room is the\nhouse by the meadow.'],
             'next': 'mark'},
            {'id': 'mark', 'op': 'set', 'state': 'canopy_stage', 'value': 1, 'next': 'done'},
            {'id': 'again', 'op': 'say', 'pages': ['Your visit is logged.\nEnjoy the canopy trail!'], 'next': 'done'},
            {'id': 'done', 'op': 'end'}]}),
        sequence(at(TRAINER), 'canopy_trainer', {**NPC, 'x': TRAINER[0], 'z': TRAINER[1], 'facing': 1,
                                           'character': f'ranger_{NEW_CHARACTERS:02d}', 'nodes': [
            {'id': 'fight', 'op': 'battle', 'trainer': 'canopy_trainer', 'won': 'done'},
            {'id': 'done', 'op': 'end'}]}),
        sequence(at(LAMP), 'canopy_lamp_ranger', {**NPC, 'x': LAMP[0], 'z': LAMP[1], 'facing': 1, 'stock_sprite': CAMPER,
                                              'presence': {'state': 'canopy_lamp', 'values': [1]}, 'nodes': [
            {'id': 'hi', 'op': 'say', 'pages': ['The canopy lamp is lit,\nso I keep watch here.'], 'next': 'done'},
            {'id': 'done', 'op': 'end'}]}),
        sequence({'header': room, 'cell': [0, 0]}, 'ranger_keeper', {**NPC, 'x': kx, 'z': kz, 'facing': 1,
                                                                    'stock_sprite': KEEPER, 'nodes': [
            {'id': 'lit', 'op': 'if', 'state': 'canopy_lamp', 'value': 1, 'yes': 'ask_off', 'no': 'ask_on'},
            {'id': 'ask_on', 'op': 'choice', 'pages': ['The canopy lamp is out.', 'Light it for the trail?'],
             'yes': 'on', 'no': 'bye'},
            {'id': 'on', 'op': 'set', 'state': 'canopy_lamp', 'value': 1, 'next': 'on_say'},
            {'id': 'on_say', 'op': 'say', 'pages': ['The lamp is lit. A ranger\nwatches it by the meadow.'], 'next': 'done'},
            {'id': 'ask_off', 'op': 'choice', 'pages': ['The canopy lamp is lit.', 'Put it out for now?'],
             'yes': 'off', 'no': 'bye'},
            {'id': 'off', 'op': 'set', 'state': 'canopy_lamp', 'value': 0, 'next': 'off_say'},
            {'id': 'off_say', 'op': 'say', 'pages': ['The lamp is out.'], 'next': 'done'},
            {'id': 'bye', 'op': 'say', 'pages': ['Come back any time.'], 'next': 'done'},
            {'id': 'done', 'op': 'end'}]})]


def encounters(project):
    """Private Canopy Walk grass table: modest early species/levels (the template's are Kanto)."""
    from sovereign_editor import gameplay
    from sovereign_editor.formats import digest
    cw = header_of(project, 'canopy_walk')
    state = project.composed()
    table = gameplay.current(project, state, gameplay.WILD, project.header(cw)['wild_pokemon'])
    edits = [{'method': 'grass', 'field': 'level', 'slot': i, 'value': v} for i, v in enumerate(ENCOUNTER_LEVELS)]
    edits += [{'method': 'grass', 'field': 'species', 'time': t, 'slot': i, 'value': v}
              for t in gameplay.TIMES for i, v in enumerate(ENCOUNTER_SPECIES)]
    return [{'kind': 'gameplay', 'context': {'header': cw, 'cell': [2, 2]}, 'request': {'operations': [
        {'kind': 'encounters', 'before_sha256': digest(table), 'edits': edits}]}}]


def rehearsal(project):
    """The final combined fixture: ONE atomic edit (layout, story and encounters)."""
    return layout(project) + rehearsal_story(project) + encounters(project)


def batches(ops, size=64):
    return [ops[i:i + size] for i in range(0, len(ops), size)]


def library_stages(project):
    stages = [('characters', characters())]
    stages += [(f'states-{n + 1}', b) for n, b in enumerate(batches(states()))]
    stages.append(('trainers', trainers()))
    stages += [(f'areas-{n + 1}', b) for n, b in enumerate(batches(areas(project)))]
    return stages


def later_stages(project):
    """Stages that need the created areas (IDs are read back from the Project)."""
    return [('presence', presence(project)), ('rehearsal', rehearsal(project))]


if __name__ == '__main__':
    from sovereign_editor.core import Project
    project = Project(sys.argv[1])
    wanted = sys.argv[2]
    found = dict(library_stages(project))
    if wanted not in found:
        found = dict(later_stages(project))
    Path(sys.argv[3]).write_text(json.dumps({'operations': found[wanted], 'label': f'Production fixture: {wanted}'},
                                            indent=1) + '\n')
    print(json.dumps({'stage': wanted, 'operations': len(found[wanted])}))
