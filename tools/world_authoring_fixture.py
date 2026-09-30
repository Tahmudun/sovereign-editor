"""World authoring v1 fixture: Survey Field (2x1) and Field Station, as area-edit operations.

A technical fixture, not story content. Every entry is an ordinary Project
operation that the World editor / `area-edit` CLI also produces; this module holds
no bytes and applies nothing. Coordinates were chosen against the composed r40
lineage (gate sites clear of stock/story events and baked trees, reachable from
the arrival gate); see docs/WORLD_AUTHORING_V1_IMPLEMENTATION.md.

Usage: world_authoring_fixture.py PROJECT OUT.json   (writes {"operations": [...]})
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sovereign_editor import gameplay, scenery, world
from sovereign_editor.formats import EditorError, digest

ROUTE = {'header': 33, 'cell': [19, 12]}
WEST, EAST = {'header': 540, 'cell': [0, 0]}, {'header': 540, 'cell': [1, 0]}
ROOM = {'header': 541, 'cell': [0, 0]}
GATE_SLOT = 0                        # Route 29 map 2: the stock Route 46 gatehouse (model 35)
ROUTE_GATE = (637, 393)              # new gatehouse on Route 29, east meadow of cell 19,12
FIELD_GATE = (5, 15)                 # Survey Field arrival gatehouse at the west end of the sand path
STATION_GATE = ((50, 5), (51, 5))    # copied Route 46 gatehouse opening in the east cell
MAT = (4, 8)                         # Field Station exit mat (copied room)
SIGN = {'slot': 0, 'from': (5, 13), 'to': (15, 13)}   # copied Route 29 sign board, moved with its collision
# East-cell local tiles (x+32): 66-tile clearing before the Field Station gate and a
# 30-tile trainer clearing; a stock ledge separates the two parts of the one edit.
CLEARING = [[18, 6], [19, 6], [18, 7], [17, 6], [19, 7], [20, 6], [17, 7], [16, 6], [19, 8], [20, 7], [17, 8],
            [16, 7], [19, 9], [20, 8], [17, 9], [16, 8], [15, 7], [19, 10], [18, 9], [20, 9], [17, 10], [16, 9],
            [15, 8], [14, 7], [19, 11], [18, 10], [20, 10], [17, 11], [16, 10], [15, 9], [14, 8], [19, 12],
            [18, 11], [20, 11], [17, 12], [15, 10], [14, 9], [13, 8], [19, 13], [18, 12], [20, 12], [17, 13],
            [16, 12], [15, 11], [14, 10], [13, 9], [19, 14], [18, 13], [20, 13], [17, 14], [16, 13], [15, 12],
            [14, 11], [13, 10], [18, 14], [17, 15], [16, 14], [15, 13], [14, 12], [13, 11], [12, 10], [17, 16],
            [15, 14], [14, 13], [13, 12], [14, 14]]
SOUTH = [[12, 26], [13, 26], [12, 25], [11, 26], [14, 26], [13, 25], [13, 27], [12, 24], [11, 25], [10, 26],
         [11, 27], [15, 26], [14, 25], [14, 27], [13, 24], [13, 28], [11, 24], [10, 25], [9, 26], [11, 28],
         [16, 26], [15, 25], [15, 27], [14, 28], [13, 23], [12, 28], [9, 25], [8, 26], [17, 26], [16, 27]]
GRASS = [[x, z] for z in (20, 21) for x in range(52, 58)]           # new tall grass (global)
TRAINER, MARKER = (44, 25), (14, 14)
CAMPER, SCIENTIST, KEEPER = 345, 338, 329                          # stock human appearances
PARTY = [{'species': 16, 'level': 5, 'moves': [33, 28, 16, 0], 'held_item': 0},
         {'species': 161, 'level': 5, 'moves': [33, 98, 111, 0], 'held_item': 155}]
ENCOUNTER_SPECIES = [161, 16, 19, 161, 16, 19, 161, 16, 19, 16, 19, 161]
ENCOUNTER_LEVELS = [2, 2, 3, 3, 3, 4, 4, 2, 3, 4, 4, 3]


STOCK_GATE = {'header': 33, 'cell': [19, 12], 'x': 624, 'z': 384}   # 6x6 footprint of the model at 627,387


def gate_footprint(context, ax, az, label):
    """Stamp the stock gatehouse's collision/behavior (walls, two 0x6E openings)."""
    return {'kind': 'terrain', 'context': context, 'request': {
        'x': ax - 3, 'z': az - 3, 'width': 6, 'height': 6, 'copy_from': dict(STOCK_GATE), 'label': label}}


def room_tile(project, trial):
    """First free flat interior tile at least three steps from the exit mat."""
    ctx = trial.context(**ROOM)
    raw = trial.member_raw(ctx['map_member'])
    for z in range(2, 12):
        for x in range(1, 10):
            at = world.cell_offset(ctx, x, z)
            if world.is_blocked(raw[at:at + 2]) or raw[at] != 0 or abs(x - MAT[0]) + abs(z - MAT[1]) < 3:
                continue
            try:
                scenery.floor_height(trial, ctx, {'x': x + .5, 'z': z + .5})
            except EditorError:
                continue
            return x, z
    raise EditorError('NOT_FOUND', 'No free Field Station tile')


def areas():
    return [
        {'kind': 'world', 'context': {'header': 33, 'cell': [18, 12]}, 'request': {
            'action': 'create', 'identity': 'survey_field', 'name': 'Survey Field', 'internal_name': 'SURVEY_FIELD',
            'template_header': 33, 'encounters': 'template', 'worldmap': [19, 12], 'close': True,
            'cells': [{'cell': [0, 0], 'source': {'header': 33, 'cell': [18, 12]}},
                      {'cell': [1, 0], 'source': {'header': 33, 'cell': [19, 12]}}],
            'label': 'Create Survey Field (2x1, private logic and encounters)'}},
        {'kind': 'world', 'context': {'header': 72, 'cell': [0, 0]}, 'request': {
            'action': 'create', 'identity': 'field_station', 'name': 'Field Station', 'internal_name': 'FIELD_STATION',
            'template_header': 72, 'encounters': 'none', 'worldmap': [19, 12], 'close': True,
            'cells': [{'cell': [0, 0], 'source': {'header': 72, 'cell': [0, 0]}}],
            'label': 'Create Field Station (private room)'}}]


def layout():
    fx, fz = FIELD_GATE
    rx, rz = ROUTE_GATE
    (sx, sz), (tx, tz) = SIGN['from'], SIGN['to']
    return [
        {'kind': 'scenery', 'context': WEST, 'request': {
            'operation': 'move', 'slot': SIGN['slot'], 'x': tx + .5, 'z': tz + .5,
            'move_collision': [{'x': sx, 'z': sz}], 'label': 'Move the copied sign board beside the survey marker'}},
        {'kind': 'scenery', 'context': ROUTE, 'request': {
            'operation': 'import', 'slot': GATE_SLOT, 'x': float(fx), 'z': float(fz), 'destination': WEST,
            'label': 'Survey Field arrival gatehouse'}},
        gate_footprint(WEST, fx, fz, 'Arrival gatehouse footprint and opening'),
        {'kind': 'scenery', 'context': ROUTE, 'request': {
            'operation': 'duplicate', 'slot': GATE_SLOT, 'x': float(rx), 'z': float(rz),
            'label': 'Route 29 gatehouse to Survey Field'}},
        gate_footprint(ROUTE, rx, rz, 'Route 29 gatehouse footprint and opening'),
        {'kind': 'world', 'context': ROUTE, 'request': {
            'action': 'connect', 'x': rx - 1, 'z': rz + 2, 'extra': [{'x': rx, 'z': rz + 2}],
            'destination': {'header': 540}, 'arrival': {'x': fx - 1, 'z': fz + 2},
            'arrival_extra': [{'x': fx, 'z': fz + 2}], 'label': 'Route 29 ↔ Survey Field gatehouses'}},
        {'kind': 'world', 'context': EAST, 'request': {
            'action': 'connect', 'x': STATION_GATE[0][0], 'z': STATION_GATE[0][1],
            'extra': [{'x': STATION_GATE[1][0], 'z': STATION_GATE[1][1]}],
            'destination': {'header': 541}, 'arrival': {'x': MAT[0], 'z': MAT[1]},
            'label': 'Survey Field gatehouse ↔ Field Station'}},
        {'kind': 'terrain', 'context': EAST, 'request': {
            'tiles': [{'x': x + 32, 'z': z} for x, z in CLEARING + SOUTH], 'material': 'road01', 'ground': 'path',
            'label': '96-tile flat path and clearings'}},
        {'kind': 'terrain', 'context': EAST, 'request': {
            'tiles': [{'x': x, 'z': z} for x, z in GRASS], 'material': 'egrass', 'ground': 'grass',
            'label': 'New tall grass (encounters)'}}]


def story(project, trial):
    rx, rz = room_tile(project, trial)
    npc = dict(kind='npc', donor_id=None, movement=0, range_x=0, range_z=0, character=None, once_state=None)
    src = {'header': 33, 'cell': [18, 12]}
    trainer = {'name': 'Reid', 'character': None, 'stock_class': 4, 'policy': 'ordinary-single-v1',
               'defeat_state': 'survey_trainer_defeated', 'party': PARTY,
               'before': ['This field is my survey\nsite! Show me your team!'],
               'after': ['Your team fits this field.'], 'revisit': ['The survey data looks good.']}
    return [
        {'kind': 'story', 'context': src, 'request': {'kind': 'state', 'key': 'survey_trainer_defeated',
                                                     'value': {'name': 'Survey Field trainer defeated'}}},
        {'kind': 'story', 'context': src, 'request': {'kind': 'state', 'key': 'survey_recorded',
                                                     'value': {'name': 'Survey recorded'}}},
        {'kind': 'story', 'context': src, 'request': {'kind': 'trainer', 'key': 'survey_trainer', 'value': trainer}},
        {'kind': 'story', 'context': EAST, 'request': {'kind': 'sequence', 'key': 'survey_trainer', 'value': {
            **npc, 'x': TRAINER[0], 'z': TRAINER[1], 'facing': 1, 'stock_sprite': CAMPER,
            'nodes': [{'id': 'fight', 'op': 'battle', 'trainer': 'survey_trainer', 'won': 'done'},
                      {'id': 'done', 'op': 'end'}]}}},
        {'kind': 'story', 'context': WEST, 'request': {'kind': 'sequence', 'key': 'survey_marker', 'value': {
            **npc, 'x': MARKER[0], 'z': MARKER[1], 'facing': 2, 'stock_sprite': SCIENTIST,
            'nodes': [{'id': 'seen', 'op': 'if', 'state': 'survey_recorded', 'value': 1, 'yes': 'again', 'no': 'ask'},
                      {'id': 'ask', 'op': 'choice', 'pages': ['This marker logs the field.', 'Record the survey now?'],
                       'yes': 'save', 'no': 'later'},
                      {'id': 'save', 'op': 'set', 'state': 'survey_recorded', 'value': 1, 'next': 'thanks'},
                      {'id': 'thanks', 'op': 'say', 'pages': ['Survey recorded!', 'Tell the Field Station.'], 'next': 'done'},
                      {'id': 'later', 'op': 'say', 'pages': ['Come back when ready.'], 'next': 'done'},
                      {'id': 'again', 'op': 'say', 'pages': ['The survey is already', 'recorded. Thanks!'], 'next': 'done'},
                      {'id': 'done', 'op': 'end'}]}}},
        {'kind': 'story', 'context': ROOM, 'request': {'kind': 'sequence', 'key': 'station_keeper', 'value': {
            **npc, 'x': rx, 'z': rz, 'facing': 1, 'stock_sprite': KEEPER,
            'nodes': [{'id': 'seen', 'op': 'if', 'state': 'survey_recorded', 'value': 1, 'yes': 'yes', 'no': 'no'},
                      {'id': 'yes', 'op': 'say', 'pages': ['Your survey arrived.', 'Field Station thanks you!'], 'next': 'done'},
                      {'id': 'no', 'op': 'say', 'pages': ['Please record the survey', 'at the marker outside.'], 'next': 'done'},
                      {'id': 'done', 'op': 'end'}]}}}]


def encounters(project, before_sha256):
    edits = [{'method': 'grass', 'field': 'level', 'slot': i, 'value': v} for i, v in enumerate(ENCOUNTER_LEVELS)]
    edits += [{'method': 'grass', 'field': 'species', 'time': t, 'slot': i, 'value': v}
              for t in gameplay.TIMES for i, v in enumerate(ENCOUNTER_SPECIES)]
    return [{'kind': 'gameplay', 'context': WEST, 'request': {'operations': [
        {'kind': 'encounters', 'before_sha256': before_sha256, 'edits': edits}]}}]


def operations(project):
    """All fixture operations, resolved against `project` (not modified)."""
    state = project.composed()
    template = gameplay.current(project, state, gameplay.WILD, project.header(33)['wild_pokemon'])
    staged = areas() + layout()
    trial = project.area_preview_project(project.plan_area_edit(areas()))
    return staged + story(project, trial) + encounters(project, digest(template))


if __name__ == '__main__':
    from sovereign_editor.core import Project
    ops = operations(Project(sys.argv[1]))
    Path(sys.argv[2]).write_text(json.dumps({'operations': ops, 'label': 'World authoring v1 fixture'}, indent=1) + '\n')
    print(json.dumps({'operations': len(ops)}))
