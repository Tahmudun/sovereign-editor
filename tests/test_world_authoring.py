"""World authoring v1: created headers, private resources and connections."""
import copy
import json
import struct
from pathlib import Path

import pytest

from sovereign_editor import world, world_authoring as wa, world_runtime as wr, event_authoring as ev
from sovereign_editor import dialogue_format as fmt, gameplay
from sovereign_editor.core import Project, atomic_json
from sovereign_editor.formats import EditorError, digest, events

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT/'projects/scyther-orchestration-1/baseline.nds'


@pytest.fixture(scope='module')
def workspace(tmp_path_factory):
    if not BASELINE.exists():
        pytest.skip('Pinned baseline is local and untracked')
    p = Project.create(BASELINE, tmp_path_factory.mktemp('world')/'project')
    return p.root, copy.deepcopy(p.doc)


@pytest.fixture
def p(workspace):
    root, doc = workspace
    atomic_json(root/'project.json', copy.deepcopy(doc))
    return Project(root)


def field(**extra):
    request = {'action': 'create', 'identity': 'survey_field', 'name': 'Survey Field',
               'internal_name': 'SURVEY_FIELD', 'template_header': 33, 'encounters': 'template',
               'worldmap': [19, 12],
               'cells': [{'cell': [0, 0], 'source': {'header': 33, 'cell': [18, 12]}},
                         {'cell': [1, 0], 'source': {'header': 33, 'cell': [19, 12]}}]}
    request.update(extra)
    return {'kind': 'world', 'context': {'header': 33, 'cell': [18, 12]}, 'request': request}


def station(**extra):
    request = {'action': 'create', 'identity': 'field_station', 'name': 'Field Station',
               'internal_name': 'FIELD_STATION', 'template_header': 72, 'encounters': 'none',
               'worldmap': [19, 12], 'cells': [{'cell': [0, 0], 'source': {'header': 72, 'cell': [0, 0]}}]}
    request.update(extra)
    return {'kind': 'world', 'context': {'header': 72}, 'request': request}


def refused(code, call):
    with pytest.raises(EditorError) as exc:
        call()
    assert exc.value.code == code, str(exc.value)
    return exc.value


def test_create_allocates_new_headers_and_private_resources(p):
    plan = p.plan_area_edit([field(), station()])
    first, second = plan['transactions']
    # New creates are planned under v2 (PROD-VIS-001); v1 history replays under v1.
    assert first['schema'] == wa.SCHEMA_V3 and second['schema'] == wa.SCHEMA_V3
    assert first['allocation'] == {'header': 540, 'matrix': 288, 'maps': [676, 677], 'events': 491,
                                   'scripts': 965, 'level_script': 966, 'text': 854, 'wild': 142}
    assert second['allocation'] == {'header': 541, 'matrix': 289, 'maps': [678], 'events': 492,
                                    'scripts': 967, 'level_script': 968, 'text': 855, 'wild': None}
    trial = p.area_preview_project(plan)
    east = trial.context(header=540, cell=[1, 0])
    stock = p.header(33)
    assert east['map_member'] == 677 and east['matrix']['id'] == 288 and east['matrix']['width'] == 2
    assert east['area_data']['area_type'] == 1 and east['name'] == 'SURVEY_FIELD'
    head = east['header']
    assert (head['event_file'], head['script_file'], head['level_script'], head['text_archive'], head['wild_pokemon']) == (491, 965, 966, 854, 142)
    for key in ('area_data', 'music_day', 'location_name', 'weather', 'camera_angle', 'follow_mode', 'flags'):
        assert head[key] == stock[key]
    assert head['worldmap']['x'] == 19 and head['worldmap']['y'] == 12
    room = trial.context(header=541)
    assert room['area_data']['area_type'] == 0 and room['header']['wild_pokemon'] == 255
    assert trial.header_count() == 542 and p.header_count() == 540


def test_created_logic_is_fresh_and_donors_are_unchanged(p):
    plan = p.plan_area_edit([field(), station()])
    trial = p.area_preview_project(plan)
    for header in (540, 541):
        h = trial.header(header)
        assert events(trial.resource(world.EVENT_ARCHIVE, h['event_file'])[1]) == {'backgrounds': [], 'npcs': [], 'warps': [], 'triggers': []}
        assert fmt.script_entries(trial.resource(fmt.SCRIPT_ARCHIVE, h['script_file'])[1])[1] == []
        assert trial.resource(fmt.SCRIPT_ARCHIVE, h['level_script'])[1][:1] == b'\0'
        assert fmt.text_entries(trial.resource(fmt.TEXT_ARCHIVE, h['text_archive'])[1])[1] == []
    assert trial.resource(gameplay.WILD, 142)[1] == p.resource(gameplay.WILD, p.header(33)['wild_pokemon'])[1]
    for ref in ({'header': 33, 'cell': [18, 12]}, {'header': 33, 'cell': [19, 12]}, {'header': 72}):
        assert trial.context(**ref)['map_sha256'] == p.context(**ref)['map_sha256']
    assert trial.header(33) == p.header(33) and trial.header(72) == p.header(72)


def test_outer_edges_close_and_the_internal_seam_is_preserved(p):
    trial = p.area_preview_project(p.plan_area_edit([field()]))
    west, east = (trial.context(header=540, cell=c) for c in ([0, 0], [1, 0]))
    donors = [p.context(header=33, cell=c) for c in ([18, 12], [19, 12])]
    for ctx, donor, outer_x in ((west, donors[0], 0), (east, donors[1], 31)):
        raw, before = trial.member_raw(ctx['map_member']), p.member_raw(donor['map_member'])
        start = ctx['sections']['permissions_offset']
        for z in range(32):
            for x in range(32):
                at = start + 2 * (32 * z + x)
                if x == outer_x or z in (0, 31):
                    assert raw[at + 1] & 0x80 and raw[at] == before[at]
                elif not (x in (0, 31)):
                    assert raw[at:at + 2] == before[at:at + 2]
        seam = 31 if outer_x == 0 else 0
        assert all(raw[start + 2*(32*z + seam):start + 2*(32*z + seam) + 2] == before[start + 2*(32*z + seam):start + 2*(32*z + seam) + 2]
                   for z in range(1, 31))


def test_apply_reopen_undo_redo_reproduce_identities(p):
    result = p.apply_area_edit(p.doc['revision'], operations=[field(), station()], label='Create areas')
    assert result['changed']
    again = Project(p.root)
    assert again.context(header=540, cell=[0, 0])['map_member'] == 676
    assert [a['identity'] for a in again.world_areas()['areas']] == ['survey_field', 'field_station']
    again.undo(again.doc['revision'])
    refused('NOT_FOUND', lambda: again.header(540))
    again.redo(again.doc['revision'])
    assert again.context(header=541)['map_member'] == 678
    assert again.world_areas()['areas'][0]['header'] == 540


def test_invalid_requests_refuse_before_any_write(p):
    before = p.path.read_bytes()
    refused('DUPLICATE_IDENTITY', lambda: p.plan_area_edit([field(), field(internal_name='OTHER')]))
    refused('INVALID_INPUT', lambda: p.plan_area_edit([field(internal_name='bad name!')]))
    refused('INVALID_INPUT', lambda: p.plan_area_edit([field(cells=[{'cell': [0, 0], 'source': {'header': 33, 'cell': [18, 12]}},
                                                                  {'cell': [0, 0], 'source': {'header': 33, 'cell': [19, 12]}}])]))
    mixed = field(cells=[{'cell': [0, 0], 'source': {'header': 72, 'cell': [0, 0]}}])
    mixed['context'] = {'header': 72}
    refused('INCOMPATIBLE_ASSETS', lambda: p.plan_area_edit([mixed]))
    refused('NO_ENCOUNTERS', lambda: p.plan_area_edit([station(encounters='template')]))
    refused('NOT_FOUND', lambda: p.plan_area_edit([field(template_header=4000)]))
    refused('STALE_REVISION', lambda: p.apply_area_edit(p.doc['revision'] + 1, operations=[field()]))
    assert p.path.read_bytes() == before


def test_created_header_capacity_is_refused_before_writing(p, monkeypatch):
    monkeypatch.setattr(wr, 'MAX_HEADERS', 1)
    before = p.path.read_bytes()
    refused('RESOURCE_CAPACITY', lambda: p.apply_area_edit(p.doc['revision'], operations=[field(), station()]))
    assert p.path.read_bytes() == before


def connect(x=50, z=5, header=541, arrival=(4, 8), source=(540, [1, 0]), extra=((51, 5),)):
    request = {'action': 'connect', 'x': x, 'z': z, 'destination': {'header': header},
               'arrival': {'x': arrival[0], 'z': arrival[1]}}
    if extra:
        request['extra'] = [{'x': a, 'z': b} for a, b in extra]
    return {'kind': 'world', 'context': {'header': source[0], 'cell': source[1]}, 'request': request}


def test_connection_adds_reciprocal_warps_on_qualified_tiles(p):
    plan = p.plan_area_edit([field(), station(), connect()])
    link = plan['transactions'][2]
    assert link['schema'] == wa.WARP_SCHEMA
    assert [(w['header'], w['id'], w['x'], w['z'], w['destination'], w['destination_warp'], w['kind']) for w in link['warps']] == [
        (540, 0, 50, 5, 541, 0, 'gate opening'), (540, 1, 51, 5, 541, 0, 'gate opening'), (541, 0, 4, 8, 540, 0, 'exit mat')]
    trial = p.area_preview_project(plan)
    outdoor = {e['id']: e for e in trial.map_events(header=540, cell=[1, 0])['events'] if e['kind'] == 'warp'}
    assert set(outdoor) == {0, 1}
    assert outdoor[0]['connection']['resolved'] and outdoor[0]['connection']['header'] == 541
    assert outdoor[0]['connection']['returns_to_source'] and not outdoor[1]['connection']['returns_to_source']
    room = [e for e in trial.map_events(header=541)['events'] if e['kind'] == 'warp']
    assert room[0]['connection']['header'] == 540 and room[0]['connection']['returns_to_source']
    raw = trial.resource(world.EVENT_ARCHIVE, 491)[1]
    assert events(raw)['warps'] == []  # created member base stays empty; warps are composed
    composed = ev.raw_member(trial, 491, trial.composed())
    assert [(w['x'], w['z'], w['destination'], w['destination_warp']) for w in events(composed)['warps']] == [(50, 5, 541, 0), (51, 5, 541, 0)]


def test_connections_refuse_unqualified_or_occupied_endpoints(p):
    base = [field(), station()]
    refused('UNSUPPORTED_ENTRANCE', lambda: p.plan_area_edit(base + [connect(x=40, z=20, extra=())]))
    refused('EVENT_CONFLICT', lambda: p.plan_area_edit(base + [connect(), connect(x=51, z=5, extra=())]))
    refused('EVENT_CONFLICT', lambda: p.plan_area_edit(base + [connect(x=626, z=389, source=(33, [19, 12]), extra=())]))
    refused('NOT_FOUND', lambda: p.plan_area_edit(base + [connect(header=4000)]))
    # Same-area links are supported (cave holes); an entrance leading onto itself is not.
    refused('INVALID_DESTINATION', lambda: p.plan_area_edit(base + [connect(header=540, arrival=(50, 5), extra=())]))
    refused('INVALID_INPUT', lambda: p.plan_area_edit(base + [connect(extra=((51, 5), (52, 5), (53, 5), (54, 5)))]))


def test_export_writes_created_areas_and_reads_back_independently(p, tmp_path):
    from sovereign_editor import world_readback
    from sovereign_editor.formats import member_count, resource
    p.apply_area_edit(p.doc['revision'], operations=[field(), station(), connect()], label='Create areas')
    first = p.export(tmp_path/'one', p.doc['revision'])
    rom = (tmp_path/'one'/'game.nds').read_bytes()
    ext = wr.decode_extension(rom)
    assert [r.hex() for r in ext['records']] == [p.header(540)['hex'], p.header(541)['hex']]
    assert world.header_count(rom) == 542 and world.header_name(rom, 541) == 'FIELD_STATION'
    counts = {a: member_count(rom, a) for a in (world.MAP_ARCHIVE, world.MATRIX_ARCHIVE, world.EVENT_ARCHIVE,
                                                  fmt.SCRIPT_ARCHIVE, fmt.TEXT_ARCHIVE, gameplay.WILD)}
    assert counts == {world.MAP_ARCHIVE: 679, world.MATRIX_ARCHIVE: 290, world.EVENT_ARCHIVE: 493,
                      fmt.SCRIPT_ARCHIVE: 969, fmt.TEXT_ARCHIVE: 856, gameplay.WILD: 143}
    assert resource(rom, world.MAP_ARCHIVE, 677)[1] == p.member_raw(677)
    assert [(w['x'], w['z'], w['destination']) for w in events(resource(rom, world.EVENT_ARCHIVE, 491)[1])['warps']] == [(50, 5, 541), (51, 5, 541)]
    assert resource(rom, world.EVENT_ARCHIVE, 30)[1] == resource(p.blob, world.EVENT_ARCHIVE, 30)[1]
    stock_reads = []
    real_read = world.read_header
    world.read_header = lambda blob, h, *a, **k: (stock_reads.append(h), real_read(blob, h, *a, **k))[1]
    try:
        report = world_readback.check(rom, p)
    finally:
        world.read_header = real_read
    assert report['failed'] == 0 and report['passed'] >= 20, [c for c in report['checks'] if not c['pass']]
    # PROD-PERF-001: the stock table is read once, not once per created header (128 at full load).
    assert len(stock_reads) <= world.header_count(p.blob) + 8, len(stock_reads)
    assert first['world']['headers'] == [540, 541]
    import sys
    sys.path[:0] = [str(ROOT/'tools'), str(ROOT/'work/tiana-fixes-1/python-tools')]
    pytest.importorskip('unicorn')
    import world_qualification as wq
    cpu = wq.cpu(rom, p.blob)
    assert cpu['failed'] == 0 and cpu['created'] == 2 and cpu['cases'] == 27 * 10
    residency = wq.residency(rom)
    assert residency['failed'] == 0 and residency['passed'] == 6, residency
    import shutil
    shutil.rmtree(tmp_path/'one')  # disk budget: one candidate ROM at a time
    second = p.export(tmp_path/'two', p.doc['revision'])
    assert second['candidate_sha256'] == first['candidate_sha256']
    shutil.rmtree(tmp_path/'two')


def free_tile(trial, header, cell, avoid=()):
    from sovereign_editor import scenery
    ctx = trial.context(header=header, cell=cell)
    ox, oz = ctx['origin']
    raw = trial.member_raw(ctx['map_member'])
    for z in range(oz + 2, oz + 30):
        for x in range(ox + 2, ox + 30):
            at = world.cell_offset(ctx, x, z)
            if world.is_blocked(raw[at:at + 2]) or raw[at] != 0 or any(abs(x - a) + abs(z - b) < 3 for a, b in avoid):
                continue
            try:
                scenery.floor_height(trial, ctx, {'x': x + .5, 'z': z + .5})
            except EditorError:
                continue
            return x, z
    raise AssertionError('no free tile')


def composition(trial, p):
    package = json.loads((ROOT/'tests/fixtures/tiana.character.json').read_text())
    from sovereign_editor import story_authoring as story, npc_behavior
    human = npc_behavior.palette(p)[0]['sprite']
    src = {'header': 33, 'cell': [18, 12]}
    west, room = {'header': 540, 'cell': [0, 0]}, {'header': 541, 'cell': [0, 0]}
    t1 = free_tile(trial, 540, [0, 0]); t2 = free_tile(trial, 540, [0, 0], avoid=[t1])
    t3 = free_tile(trial, 540, [0, 0], avoid=[t1, t2]); t4 = free_tile(trial, 541, [0, 0], avoid=[(4, 8)])
    trainer = {'name': 'Surveyor', 'character': None, 'stock_class': 3, 'policy': story.ORDINARY,
               'defeat_state': 'survey_trainer_defeated',
               'party': [{'species': 16, 'level': 4, 'moves': [16, 33, 28, 0], 'held_item': 0}],
               'before': ['Survey battle!'], 'after': ['Well fought.'], 'revisit': ['The field is yours.']}
    npc = dict(kind='npc', donor_id=None, facing=0, movement=0, range_x=0, range_z=0, character=None, once_state=None)
    return [
        {'kind': 'story', 'context': src, 'request': {'kind': 'character', 'key': 'tiana', 'value': package}},
        {'kind': 'story', 'context': src, 'request': {'kind': 'state', 'key': 'survey_trainer_defeated', 'value': {'name': 'Survey trainer'}}},
        {'kind': 'story', 'context': src, 'request': {'kind': 'state', 'key': 'survey_recorded', 'value': {'name': 'Survey recorded'}}},
        {'kind': 'story', 'context': src, 'request': {'kind': 'trainer', 'key': 'surveyor', 'value': trainer}},
        {'kind': 'story', 'context': west, 'request': {'kind': 'sequence', 'key': 'survey_trainer', 'value': {
            **npc, 'x': t1[0], 'z': t1[1], 'stock_sprite': human,
            'nodes': [{'id': 'fight', 'op': 'battle', 'trainer': 'surveyor', 'won': 'done'}, {'id': 'done', 'op': 'end'}]}}},
        {'kind': 'story', 'context': west, 'request': {'kind': 'sequence', 'key': 'survey_marker', 'value': {
            **npc, 'x': t2[0], 'z': t2[1], 'stock_sprite': human,
            'nodes': [{'id': 'seen', 'op': 'if', 'state': 'survey_recorded', 'value': 1, 'yes': 'again', 'no': 'ask'},
                      {'id': 'ask', 'op': 'choice', 'pages': ['Record the survey?'], 'yes': 'save', 'no': 'done'},
                      {'id': 'save', 'op': 'set', 'state': 'survey_recorded', 'value': 1, 'next': 'thanks'},
                      {'id': 'thanks', 'op': 'say', 'pages': ['Survey recorded.'], 'next': 'done'},
                      {'id': 'again', 'op': 'say', 'pages': ['Already recorded.'], 'next': 'done'},
                      {'id': 'done', 'op': 'end'}]}}},
        {'kind': 'story', 'context': room, 'request': {'kind': 'sequence', 'key': 'station_keeper', 'value': {
            **npc, 'x': t4[0], 'z': t4[1], 'stock_sprite': human,
            'nodes': [{'id': 'seen', 'op': 'if', 'state': 'survey_recorded', 'value': 1, 'yes': 'yes', 'no': 'no'},
                      {'id': 'yes', 'op': 'say', 'pages': ['Thanks for the survey!'], 'next': 'done'},
                      {'id': 'no', 'op': 'say', 'pages': ['Please record the survey.'], 'next': 'done'},
                      {'id': 'done', 'op': 'end'}]}}},
        {'kind': 'interaction', 'context': west, 'request': {'action': 'create', 'kind': 'npc', 'donor_id': None,
            'x': t3[0], 'z': t3[1], 'facing': 0, 'dialogue': 'A quiet field.', 'sprite': human}},
    ]


def test_existing_story_gameplay_and_interaction_tools_compose_in_created_areas(p, tmp_path):
    from sovereign_editor.formats import resource
    base = [field(), station(), connect()]
    trial = p.area_preview_project(p.plan_area_edit(base))
    ops = composition(trial, p)
    raw = trial.resource(gameplay.WILD, 142)[1]
    ops.append({'kind': 'gameplay', 'context': {'header': 540, 'cell': [0, 0]}, 'request': {'operations': [
        {'kind': 'encounters', 'before_sha256': digest(raw), 'edits': [
            {'method': 'grass', 'field': 'species', 'time': 'day', 'slot': 0, 'value': 16},
            {'method': 'grass', 'field': 'level', 'slot': 0, 'value': 3}]}]}})
    result = p.apply_area_edit(p.doc['revision'], operations=base + ops, label='Survey fixture')
    assert result['changed']
    state = p.composed()
    seqs = state['story']['sequence']
    assert {k: v['event_member'] for k, v in seqs.items()} == {'survey_trainer': 491, 'survey_marker': 491, 'station_keeper': 492}
    assert all(v['y'] == 0 for v in seqs.values())
    rows = events(ev.raw_member(p, 491, state))
    assert len(rows['npcs']) == 3 and len(rows['warps']) == 2
    assert p.gameplay_data(540)['encounters']['member'] == 142
    assert p.gameplay_data(540)['encounters']['methods']['grass']['day'][0] == 16
    assert p.gameplay_data(33)['encounters']['methods']['grass'] == gameplay.decode_wild(p.resource(gameplay.WILD, 1)[1])['grass']
    out = p.export(tmp_path/'rom', p.doc['revision'])
    rom = (tmp_path/'rom'/'game.nds').read_bytes()
    assert len(fmt.script_entries(resource(rom, fmt.SCRIPT_ARCHIVE, 965)[1])[1]) == 3
    assert len(fmt.script_entries(resource(rom, fmt.SCRIPT_ARCHIVE, 967)[1])[1]) == 1
    assert len(fmt.text_entries(resource(rom, fmt.TEXT_ARCHIVE, 854)[1])[1]) >= 6
    assert gameplay.decode_wild(resource(rom, gameplay.WILD, 142)[1])['grass']['day'][0] == 16
    from sovereign_editor import world_readback
    report = world_readback.check(rom, p)
    assert report['failed'] == 0, [c for c in report['checks'] if not c['pass']]
    import shutil
    shutil.rmtree(tmp_path/'rom')


def test_donorless_events_are_limited_to_created_areas(p):
    from sovereign_editor import npc_behavior
    human = npc_behavior.palette(p)[0]['sprite']
    refused('NOT_FOUND', lambda: p.plan_area_edit([{'kind': 'interaction', 'context': {'header': 33, 'cell': [18, 12]},
        'request': {'action': 'create', 'kind': 'npc', 'donor_id': None, 'x': 584, 'z': 398, 'facing': 0,
                    'dialogue': 'Hi.', 'sprite': human}}]))


PATH96 = [[1, 6], [2, 6], [3, 6], [16, 6], [17, 6], [18, 6], [19, 6], [20, 6], [3, 7], [4, 7], [14, 7], [15, 7], [16, 7], [17, 7],
          [18, 7], [19, 7], [20, 7], [3, 8], [4, 8], [5, 8], [6, 8], [13, 8], [14, 8], [15, 8], [16, 8], [17, 8], [19, 8], [20, 8],
          [5, 9], [6, 9], [13, 9], [14, 9], [15, 9], [16, 9], [17, 9], [18, 9], [19, 9], [20, 9], [5, 10], [6, 10], [7, 10], [8, 10],
          [9, 10], [10, 10], [12, 10], [13, 10], [14, 10], [15, 10], [16, 10], [17, 10], [18, 10], [19, 10], [20, 10], [7, 11],
          [8, 11], [13, 11], [14, 11], [15, 11], [17, 11], [18, 11], [19, 11], [20, 11], [8, 12], [9, 12], [13, 12], [14, 12],
          [15, 12], [16, 12], [17, 12], [18, 12], [19, 12], [20, 12], [11, 13], [14, 13], [15, 13], [16, 13], [17, 13], [18, 13],
          [19, 13], [20, 13], [11, 14], [12, 14], [14, 14], [15, 14], [16, 14], [17, 14], [18, 14], [19, 14], [17, 15], [4, 16],
          [5, 16], [17, 16], [4, 17], [5, 17], [6, 17], [1, 18]]
EAST = {'header': 540, 'cell': [1, 0]}


def terrain(ctx=EAST, **request):
    return {'kind': 'terrain', 'context': ctx, 'request': request}


def tiles(local, dx=32):
    return [{'x': x + dx, 'z': z} for x, z in local]


def model_counts(project, ctx):
    from sovereign_editor import surface_authoring, surface_format
    model = surface_authoring.model(project, project.context(**ctx), project.composed())
    _, _, mdl, _, _ = surface_format.layout(model)
    return struct.unpack_from('<4H', mdl, 36), len(model)


def test_terrain_paints_a_96_tile_path_within_the_measured_geometry_budget(p):
    from sovereign_editor import surface_authoring as sa
    base = [field(), station()]
    plan = p.plan_area_edit(base + [terrain(tiles=tiles(PATH96), material='road01', ground='path')])
    surface = [t for t in plan['transactions'] if t['schema'] in sa.SCHEMAS]
    assert len(surface) == 1 and surface[0]['schema'] == sa.LARGE_SCHEMA and len(surface[0]['cells']) == 96
    trial = p.area_preview_project(plan)
    (verts, polys, tris, quads), size = model_counts(trial, EAST)
    before = model_counts(p.area_preview_project(p.plan_area_edit(base)), EAST)
    assert polys > before[0][1] and polys <= sa.BUDGET['polygons'] and verts <= sa.BUDGET['vertices'] and size <= 0xF000
    donor = p.context(header=33, cell=[19, 12])
    assert trial.context(header=33, cell=[19, 12])['map_sha256'] == donor['map_sha256']
    from sovereign_editor.surface_native import sample_mapping
    from sovereign_editor import mapscene
    _, blobs = mapscene.tilesets(trial, trial.context(**EAST))
    model = sa.model(trial, trial.context(**EAST), trial.composed())
    assert all(sample_mapping(model, blobs['map_tileset'], x, z)['material'] == 'road01' for x, z in PATH96[::7])


def test_terrain_refuses_oversized_over_budget_and_legacy_limits(p, monkeypatch):
    from sovereign_editor import surface_authoring as sa
    base = [field(), station()]
    refused('INVALID_INPUT', lambda: p.plan_area_edit(base + [terrain(tiles=tiles([[x, z] for x in range(32) for z in range(9)]), material='road01')]))
    refused('INVALID_INPUT', lambda: p.plan_area_edit(base + [terrain(x=40, z=6, width=17, height=1, material='road01')]))
    refused('INVALID_INPUT', lambda: p.plan_area_edit(base + [terrain(x=40, z=6, width=2, height=1)]))
    monkeypatch.setattr(sa, 'BUDGET', {'polygons': 320, 'vertices': 9999})
    refused('RESOURCE_CAPACITY', lambda: p.plan_area_edit(base + [terrain(tiles=tiles(PATH96), material='road01')]))
    monkeypatch.undo()
    # Pre-v4 surface requests keep their historical 8x8 rectangle and 64-tile limits.
    refused('INVALID_INPUT', lambda: p.plan_area_edit(base + [{'kind': 'surface', 'context': EAST, 'request': {
        'x': 40, 'z': 6, 'width': 9, 'height': 1, 'material': 'road01'}}]))


def test_tall_grass_needs_visible_grass_and_an_encounter_table(p):
    base = [field(), station()]
    patch = tiles([[20, 20], [21, 20], [22, 20], [20, 21], [21, 21], [22, 21]])
    refused('UNSUPPORTED_TERRAIN', lambda: p.plan_area_edit(base + [terrain(tiles=patch, ground='grass')]))
    plan = p.plan_area_edit(base + [terrain(tiles=patch, material='egrass', ground='grass')])
    trial = p.area_preview_project(plan)
    cells = trial.permission_cells(**EAST, x=52, z=20, width=3, height=2)['cells']
    assert all(c['type'] == 2 and not c['blocked'] for c in cells)
    from sovereign_editor.surface_native import sample_mapping
    from sovereign_editor import mapscene, surface_authoring as sa
    _, blobs = mapscene.tilesets(trial, trial.context(**EAST))
    model = sa.model(trial, trial.context(**EAST), trial.composed())
    top = sample_mapping(model, blobs['map_tileset'], 21, 20)
    assert top['material'] == 'egrass' and abs(top['height'] - 18) < 1e-6
    assert sample_mapping(model, blobs['map_tileset'], 23, 20)['material'] != 'egrass'
    room = {'header': 541, 'cell': [0, 0]}
    refused('NO_ENCOUNTERS', lambda: p.plan_area_edit(base + [terrain(ctx=room, x=3, z=6, width=1, height=1, ground='grass')]))


def test_terrain_collision_edits_are_explicit_and_spare_entrances(p):
    base = [field(), station(), connect()]
    plan = p.plan_area_edit(base + [terrain(x=44, z=12, width=3, height=1, blocked=True)])
    trial = p.area_preview_project(plan)
    assert all(c['blocked'] for c in trial.permission_cells(**EAST, x=44, z=12, width=3, height=1)['cells'])
    refused('UNSUPPORTED_TERRAIN', lambda: p.plan_area_edit(base + [terrain(x=50, z=5, width=1, height=1, blocked=True)]))


def test_cli_previews_applies_and_lists_created_areas(p, tmp_path, capsys):
    from sovereign_editor import cli
    request = tmp_path/'request.json'
    request.write_text(json.dumps({'operations': [field(), station(), connect()], 'label': 'Survey areas'}))
    assert cli.main(['area-edit', '--project', str(p.root), '--request', str(request), '--dry-run']) == 0
    preview = json.loads(capsys.readouterr().out)['result']
    assert [c['operation'] for c in preview['preview']] == ['world.area', 'world.area', 'world.connection']
    assert preview['preview'][0]['allocation']['header'] == 540 and Project(p.root).doc['revision'] == p.doc['revision']
    assert cli.main(['area-edit', '--project', str(p.root), '--request', str(request), '--revision', str(p.doc['revision'])]) == 0
    capsys.readouterr()
    assert cli.main(['world-areas', '--project', str(p.root)]) == 0
    listed = json.loads(capsys.readouterr().out)['result']
    assert [a['identity'] for a in listed['areas']] == ['survey_field', 'field_station']
    assert listed['areas'][0]['owners']['encounters'] == 142 and listed['capacity']['next_header'] == 542
    assert len(listed['connections']) == 1


def test_wide_entrances_on_both_sides_return_to_one_warp(p):
    # Survey Field's copied gate (two 0x6E tiles) to a copy placed on Route 29 cell 19,12.
    gate = {'kind': 'scenery', 'context': {'header': 540, 'cell': [1, 0]},
            'request': {'operation': 'import', 'slot': 0, 'x': 637.0, 'z': 393.0, 'destination': {'header': 33, 'cell': [19, 12]}}}
    foot = [{'x': x, 'z': z, 'after': '6e06' if (x, z) in ((636, 395), (637, 395)) else '0080'}
            for x in range(634, 640) for z in range(390, 396)]
    ops = [field(), gate, {'kind': 'map', 'context': {'header': 33, 'cell': [19, 12]}, 'request': {'permissions': foot}},
           connect(x=50, z=5, header=33, extra=((51, 5),), arrival=(636, 395))]
    ops[-1]['request']['arrival_extra'] = [{'x': 637, 'z': 395}]
    plan = p.plan_area_edit(ops)
    link = plan['transactions'][-1]
    assert [(w['header'], w['id'], w['x'], w['z'], w['destination'], w['destination_warp']) for w in link['warps']] == [
        (540, 0, 50, 5, 33, 2), (540, 1, 51, 5, 33, 2), (33, 2, 636, 395, 540, 0), (33, 3, 637, 395, 540, 0)]
    trial = p.area_preview_project(plan)
    route = {e['id']: e for e in trial.map_events(header=33, cell=[19, 12])['events'] if e['kind'] == 'warp'}
    assert route[2]['connection']['returns_to_source'] and route[3]['connection']['header'] == 540
    assert route[0]['connection']['header'] == 134  # the stock Route 46 gate is untouched
    refused('INVALID_INPUT', lambda: p.plan_area_edit(ops[:3] + [{**ops[-1], 'request': {**ops[-1]['request'],
            'arrival_extra': [{'x': 636, 'z': 395}]}}]))


def test_batch_planning_composes_incrementally_like_a_full_replay(p):
    from sovereign_editor import world_authoring as w
    ops = [field(), station(), connect(), terrain(tiles=tiles(PATH96[:20]), material='road01', ground='path')]
    plan = p.plan_area_edit(ops)
    trial = copy.copy(p); trial.doc = copy.deepcopy(p.doc); trial._composed_cache = None
    trial.doc['map_edits'] = trial.doc['map_edits'] + plan['transactions']
    full = trial.composed()
    incremental = copy.copy(p); incremental.doc = copy.deepcopy(p.doc); incremental._composed_cache = None
    incremental.composed()
    for t in plan['transactions']:
        incremental.doc['map_edits'].append(t)
        incremental._append_composed(t)
    fast = incremental._composed_cache
    for key in ('permissions', 'surfaces', 'surface_versions', 'world', 'world_warps', 'event_records'):
        assert fast.get(key) == full.get(key), key
    assert incremental._world_headers == trial._world_headers and incremental._room_members == trial._room_members


def test_terrain_stamps_a_stock_footprint_exactly(p):
    stamp = terrain(ctx={'header': 540, 'cell': [0, 0]}, x=2, z=12, width=6, height=6,
                    copy_from={'header': 33, 'cell': [19, 12], 'x': 624, 'z': 384}, label='Gatehouse footprint')
    trial = p.area_preview_project(p.plan_area_edit([field(), stamp]))
    got = [c['value'] for c in trial.permission_cells(header=540, cell=[0, 0], x=2, z=12, width=6, height=6)['cells']]
    source = [c['value'] for c in p.permission_cells(header=33, cell=[19, 12], x=624, z=384, width=6, height=6)['cells']]
    assert got == source and got.count('6e06') == 2
    refused('INVALID_INPUT', lambda: p.plan_area_edit([field(), {**stamp, 'request': {**stamp['request'], 'material': 'road01'}}]))
    refused('INVALID_INPUT', lambda: p.plan_area_edit([field(), {**stamp, 'request': {**stamp['request'], 'width': 17}}]))
    onto = terrain(ctx=EAST, x=48, z=0, width=6, height=6, copy_from={'header': 33, 'cell': [19, 12], 'x': 624, 'z': 384})
    refused('UNSUPPORTED_TERRAIN', lambda: p.plan_area_edit([field(), station(), connect(), onto]))
