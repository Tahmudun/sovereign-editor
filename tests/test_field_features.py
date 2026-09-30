"""Native field-move features (FIELD-02 Whirlpool). Software checks only."""
import struct
from pathlib import Path

import pytest

from sovereign_editor import field_features as ff, scenery
from sovereign_editor.core import Project
from sovereign_editor.formats import EditorError, resource, map_data

ROOT = Path(__file__).resolve().parents[1]
PARENT = ROOT / 'projects/editor-completion-v1'
pytestmark = pytest.mark.skipif(not PARENT.is_dir(), reason='r82 parent absent')
BAY = {'header': 67, 'cell': [16, 12]}          # Cherrygrove's southern sea (stock member 4)


def op(request):
    return {'kind': 'field', 'context': BAY, 'request': request}


def test_stock_whirlpools_match_the_qualified_shape():
    blob = Project(PARENT).blob
    for member in (14, 15, 64, 65, 66, 67):
        coll, props, _ = map_data(resource(blob, 'a/0/6/5', member)[1])
        tiles = [(i // 2 % 32, i // 2 // 32) for i in range(0, len(coll), 2) if coll[i] == 0x11]
        assert len(tiles) == 9 and all(coll[2 * (z * 32 + x) + 1] == 0x80 for x, z in tiles)
        xs, zs = sorted({t[0] for t in tiles}), sorted({t[1] for t in tiles})
        record = next(p for p in props if p['model_id'] == ff.WHIRLPOOL['model'])
        assert record['xyz'] == [xs[1] + 0.5 - 16, 0.5, zs[1] + 0.5 - 16]
        assert record['rotation_raw'] == [0, 0, 0] and record['scale_raw'] == [4096] * 3


def test_whirlpool_place_refuse_remove(tmp_path):
    p = Project(PARENT).clone(tmp_path / 'p')
    with pytest.raises(EditorError, match='not open Surf water'):
        p.plan_area_edit([op({'action': 'place', 'key': 'w', 'family': 'whirlpool', 'x': 520, 'z': 390})])
    p.apply_area_edit(p.doc['revision'], operations=[op({'action': 'place', 'key': 'bay', 'family': 'whirlpool',
                                                         'x': 528, 'z': 393})])
    state = p.composed()
    spec = ff.features(state)['bay']
    assert len(spec['tiles']) == 9 and spec['height'] == 0.5 and len(spec['crossings']) == 6
    ctx = p.context(**BAY)
    table = scenery.table_for(p, ctx, state)
    raw = table[spec['slot']]['raw']
    assert struct.unpack_from('<I3i', raw) == (43, (529.5 - 512 - 16) * 65536, 32768, (394.5 - 384 - 16) * 65536)
    for t in spec['tiles']:
        assert state['permissions'][(ctx['map_member'], t['offset'])] == bytes((0x11, 0x80))
    with pytest.raises(EditorError, match='not open Surf water'):     # overlapping second whirlpool
        p.plan_area_edit([op({'action': 'place', 'key': 'b2', 'family': 'whirlpool', 'x': 529, 'z': 393})])
    reopened = Project(p.root)
    assert ff.features(reopened.composed()) == ff.features(state)
    p.apply_area_edit(p.doc['revision'], operations=[op({'action': 'remove', 'key': 'bay'})])
    state = p.composed()
    assert 'bay' not in ff.features(state)
    assert spec['slot'] not in scenery.table_for(p, ctx, state)
    for t in spec['tiles']:
        assert state['permissions'][(ctx['map_member'], t['offset'])] == bytes.fromhex(t['before'])
    p.undo(p.doc['revision'])
    assert 'bay' in ff.features(p.composed())


RIDGE = 668                                     # ec_ridge (Route 39 copy) in the r82 parent
CLIMB = {'action': 'terrace_shape', 'rects': [[20, 16, 2, 2]], 'level': 1, 'access': [], 'ledges': [],
         'climbs': [{'side': 'south', 'x': 20, 'z': 18}], 'label': 'Climb test'}


def test_rock_climb_face_behavior_height_and_texture():
    from sovereign_editor import nitro, terrain_authoring as ta, terrain_geometry as tg, travel, mapscene
    p = Project(PARENT)
    plan = p.plan_area_edit([{'kind': 'elevation', 'context': {'header': RIDGE, 'cell': [0, 0]}, 'request': CLIMB}])
    report = next(t['report'] for t in plan['transactions'] if t.get('report'))
    assert report['climbs'] == [{'side': 'south', 'tile': [20, 18], 'landing': [20, 19],
                                 'behavior': '0x4b Rock Climb (Earth Badge, std 10010)', 'landing_reachable': True}]
    trial = p.area_preview_project(plan)
    state = trial.composed()
    ctx, pair, height = travel._tile(trial, state, RIDGE, 20, 18)
    assert pair == bytes((0x4B, 0x80)) and height == pytest.approx(4.5, abs=0.01)
    _, _, top = travel._tile(trial, state, RIDGE, 20, 17)
    assert top == pytest.approx(5.0)
    from sovereign_editor import surface_authoring
    def climb_vertices(proj):
        _, blobs = mapscene.tilesets(proj, ctx, proj.composed())
        _, prims = nitro.decode_model(surface_authoring.model(proj, ctx, proj.composed()), tileset=blobs['map_tileset'],
                                      render=False)
        return [v for pr in prims if pr.material.get('texture_name') == tg.CLIMB_MATERIAL for v in pr.vertices
                if 64 - 1 <= v[0] <= 80 + 1 and 32 - 8 <= v[2] <= 48 + 8]
    assert climb_vertices(trial) and not climb_vertices(p)       # the rim tile (20, 18) now shows r_climb
    # A corner is not a straight rim tile.
    with pytest.raises(EditorError, match='not a straight south rim tile'):
        p.plan_area_edit([{'kind': 'elevation', 'context': {'header': RIDGE, 'cell': [0, 0]},
                           'request': {**CLIMB, 'climbs': [{'side': 'south', 'x': 19, 'z': 18}]}}])


def test_flash_darkness_on_a_created_cave():
    """FIELD-04: identity weather 11 makes a created cave dark; Flash (std 10013) sets FLAG_SYS_FLASH and
    weather 12; the flag survives cave-to-cave warps (map type 3) and clears in any other map."""
    p = Project(PARENT)
    cave = p.header(670)
    assert cave['name'] == 'EC_CAVE' and cave['location_type'] == 3 and cave['weather'] == 0
    plan = p.plan_area_edit([{'kind': 'world', 'context': {'header': 670, 'cell': [0, 0]},
                              'request': {'action': 'identity', 'area': 'ec_cave', 'weather': 11}}])
    assert p.area_preview_project(plan).header(670)['weather'] == 11


def test_waterfall_rows_heights_and_task_geometry():
    """FIELD-01: the stock task climbs two tiles north onto water from a 0x13 row (and descends two
    south); the feature gives lower water, the fall row and upper water exactly that shape."""
    from sovereign_editor import travel
    p = Project(PARENT)
    create = {'kind': 'world', 'context': {'header': 33, 'cell': [19, 12]}, 'request': {
        'action': 'create', 'identity': 'falls_test', 'name': 'Falls Test', 'internal_name': 'FALLS_TEST',
        'template_header': 33, 'encounters': 'none', 'worldmap': [19, 12],
        'cells': [{'cell': [0, 0], 'source': {'header': 33, 'cell': [19, 12]}}]}}
    trial = p.area_preview_project(p.plan_area_edit([create]))
    h = max(a['header'] for a in trial.world_areas()['areas'])
    link = {'kind': 'world', 'context': {'header': h, 'cell': [0, 0]}, 'request': {
        'action': 'connect', 'x': 18, 'z': 5, 'destination': {'header': 543}, 'arrival': {'x': 4, 'z': 8}}}
    trial = trial.area_preview_project(trial.plan_area_edit([link]))
    fall = {'kind': 'elevation', 'context': {'header': h, 'cell': [0, 0]}, 'request': {
        'action': 'waterfall', 'x': 15, 'z': 9, 'width': 3, 'upper': 2, 'lower': 2, 'label': 'Falls'}}
    plan = trial.plan_area_edit([fall])
    report = next(t['report'] for t in plan['transactions'] if t.get('report'))
    assert report['fall'] == [[15, 11], [16, 11], [17, 11]] and report['lower_reachable']
    done = trial.area_preview_project(plan)
    state = done.composed()
    for x in (15, 16, 17):
        _, pair, fall_h = travel._tile(done, state, h, x, 11)
        _, lower, lower_h = travel._tile(done, state, h, x, 12)
        _, upper, upper_h = travel._tile(done, state, h, x, 10)
        # Geometry v2 (R101-WATERFALL): the fall row is blocked like stock D38; the Waterfall
        # move crosses it (the reachability flood links lower and upper water through it).
        assert pair == bytes((0x13, 0x80)) and lower == upper == bytes((0x15, 0x00))
        assert (lower_h, fall_h, upper_h) == pytest.approx((0.5, 1.5, 1.5), abs=1e-3)
    assert report['geometry_version'] == 2 and report['upper_reachable_via_fall']
    _, rim, _ = travel._tile(done, state, h, 14, 11)
    assert rim == bytes((0x00, 0x80))


R101 = ROOT / 'projects/original-content-v1'


@pytest.mark.skipif(not R101.is_dir(), reason='r101 parent absent')
def test_waterfall_v2_revision_blocks_the_row_and_animates_the_chute(tmp_path):
    """R101-WATERFALL: the recorded v1 fall (walkable 13 00 on a one-level slope, water as new static
    materials) replays unchanged; a revision keeps the pools, blocks the row (13 80), draws the pools,
    chute and foam on the model's area-animation slots and adds the stock waterfall textures."""
    import struct
    from sovereign_editor import ground_materials as gm, mapscene, nitro, nitro_writer, surface_authoring, travel
    from sovereign_editor.formats import EditorError, resource
    p = Project(R101).clone(tmp_path / 'p')
    state = p.composed()
    fall = next(f for f in state['terrain_features'][824] if f['id'] == 'waterfall@15,9')
    assert 'version' not in fall['spec']
    assert travel._tile(p, state, 671, 16, 11)[1] == bytes((0x13, 0x00))
    request = {'action': 'waterfall', 'x': 15, 'z': 9, 'width': 3, 'upper': 2, 'lower': 2,
               'label': 'Blossom Falls waterfall', 'replaces': 'waterfall@15,9'}
    op = lambda r: [{'kind': 'elevation', 'context': {'header': 671, 'cell': [0, 0]}, 'request': r}]
    for bad, code in (({**request, 'width': 4}, 'UNSUPPORTED_TERRAIN'), ({**request, 'replaces': 'waterfall@1,1'}, 'NOT_FOUND'),
                      ({**request, 'version': 1}, 'INVALID_INPUT')):
        with pytest.raises(EditorError) as exc:
            p.plan_area_edit(op(bad))
        assert exc.value.code == code
    plan = p.plan_area_edit(op(request))
    terrain, collision = plan['transactions']
    assert terrain['request']['version'] == 2 and terrain['report']['upper_reachable_via_fall']
    assert [(c['x'], c['z'], c['before'], c['after']) for c in collision['permissions']] == \
        [(x, 11, '1300', '1380') for x in (15, 16, 17)]
    p.apply_area_edit(p.doc['revision'], operations=op(request))
    with pytest.raises(EditorError) as exc:
        p.plan_area_edit(op({**request, 'replaces': 'waterfall@15,9'}))
    assert exc.value.code == 'NO_CHANGE'
    state = p.composed()
    ctx = p.context(header=671, cell=[0, 0])
    raw = surface_authoring.model(p, ctx, state)
    blk = nitro.blocks(raw)[b'MDL0']
    model = blk[struct.unpack('<I', nitro.info(blk, 8)[0][1])[0]:]
    _, _, mat_off, shp_off, _ = struct.unpack_from('<5I', model)
    mats = model[mat_off:shp_off]
    names = [n for n, _ in nitro_writer.read_dictionary(mats, 4)[0]]
    bound = {}
    tex_dict = struct.unpack_from('<H', mats)[0]
    for name, v in nitro_writer.read_dictionary(mats, tex_dict)[0]:
        start, count, _ = struct.unpack('<HBB', v)
        for i in mats[start:start + count]:
            bound[i] = name
    assert [(names[i], bound.get(i)) for i in range(4, 8)] == \
        [('sea_un', 'sea_un'), ('sea_on', 'sea_on'), ('river', 'river'), ('river_r', 'river_r')]
    textures, _ = nitro.texture_set(mapscene.tilesets(p, ctx, state)[1]['map_tileset'])
    assert (textures['river_r'].width, textures['river'].width, textures['river'].isColor0Transparent) == (64, 16, True)
    wfall = gm._stock_texture(resource(p.blob, 'a/0/7/0', 18)[1], 'wfall', 'wfall_pl')[0]
    chute = gm.fall_textures(p)[0]['indices']
    assert chute == [wfall[x * 64 + y] for y in range(64) for x in range(64)]
    plan = gm.export_plan(p, state)
    area2 = next(a for a in plan['report']['areas'] if a['area_data'] == 2)
    assert {'river_r', 'river'} <= set(area2['textures'])
    assert area2['texture_vram_bytes'] <= area2['stock_outdoor_maximum']
    # Undo restores the recorded v1 fall row.
    p.undo(p.doc['revision'])
    assert travel._tile(p, p.composed(), 671, 16, 11)[1] == bytes((0x13, 0x00))
