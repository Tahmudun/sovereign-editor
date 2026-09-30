"""Terrain v2 shapes (TERRAIN-01/02): concave terraces with inner corners, a second level,
stairs by tile, one-way ledges, and concave water shores. Software checks only."""
import struct
from pathlib import Path

import numpy as np
import pytest

from sovereign_editor import terrain_authoring as ta, terrain_geometry as tg, world_authoring as wa
from sovereign_editor.core import Project
from sovereign_editor.formats import EditorError

ROOT = Path(__file__).resolve().parents[1]
PARENT = ROOT / 'projects/assets-gameplay-v1'
pytestmark = pytest.mark.skipif(not PARENT.is_dir(), reason='returned assets-gameplay-v1 (r64) parent absent')

# Stock Route 39 cell (8,5): flat ground at 4.0 (x 3..16, z 5..25), barn/house doors at (4,4)/(9,4).
RIDGE = {'kind': 'world', 'context': {'header': 43, 'cell': [8, 5]}, 'request': {
    'action': 'create', 'identity': 'ec_ridge', 'name': 'Ridge Trail', 'internal_name': 'EC_RIDGE',
    'template_header': 43, 'encounters': 'none', 'worldmap': [19, 12],
    'cells': [{'cell': [0, 0], 'source': {'header': 43, 'cell': [8, 5]}}]}}
HUT = {'kind': 'world', 'context': {'header': 72}, 'request': {
    'action': 'create', 'identity': 'ec_hut', 'name': 'Ridge Hut', 'internal_name': 'EC_HUT',
    'template_header': 72, 'encounters': 'none', 'worldmap': [19, 12],
    'cells': [{'cell': [0, 0], 'source': {'header': 72, 'cell': [0, 0]}}]}}
LOWER = {'action': 'terrace_shape', 'rects': [[5, 13, 8, 4], [5, 17, 4, 2]], 'level': 1,
         'access': [{'side': 'south', 'x': 5, 'z': 19}],
         'ledges': [{'side': 'south', 'x': 10, 'z': 17, 'length': 3}], 'label': 'Lower ridge'}
UPPER = {'action': 'terrace_shape', 'rects': [[7, 14, 3, 1]], 'level': 2,
         'access': [{'side': 'south', 'x': 7, 'z': 15}], 'ledges': [], 'label': 'Upper ridge'}
# Canopy Walk south lawn (cells [3,2]): an L of Surf water with a concave corner at (106,81).
RIVER = {'action': 'water_shape', 'rects': [[107, 77, 3, 7], [99, 82, 8, 2]], 'traversable': True, 'label': 'Bend'}


def fresh(tmp_path):
    return Project(PARENT).clone(tmp_path / 'p')


def ridge_project(tmp_path):
    p = fresh(tmp_path)
    p.apply_area_edit(64, operations=[RIDGE, HUT], label='ridge')
    areas = {a['identity']: a['header'] for a in p.world_areas()['areas']}
    ridge, hut = areas['ec_ridge'], areas['ec_hut']
    link = {'kind': 'world', 'context': {'header': ridge, 'cell': [0, 0]},
            'request': {'action': 'connect', 'x': 4, 'z': 4, 'destination': {'header': hut}, 'arrival': {'x': 4, 'z': 8}}}
    p.apply_area_edit(65, operations=[link], label='ridge door')
    return p, ridge


def elevation(header, spec):
    return {'kind': 'elevation', 'context': {'header': header, 'cell': [0, 0] if header != 542 else [3, 2]},
            'request': spec}


def test_rim_classification_and_shape_refusals():
    roles = tg.classify_rim(tg.rect_tiles(LOWER['rects']))
    assert roles[(9, 17)] == ('inner', ('east', 'south'), (9, 17))        # concave vertex at the tile's NW corner
    assert roles[(4, 12)] == ('outer', 'north_west') and roles[(13, 17)] == ('outer', 'south_east')
    assert roles[(11, 17)] == ('side', 'south') and roles[(9, 18)] == ('side', 'east')
    lay = tg.shaped_terrace_layout(ta.normalise(LOWER))
    assert [t for s in lay['stairs'] for t in s['tiles']] == [(5, 19), (6, 19), (7, 19)]
    assert lay['ledges'][0]['landing'] == [(10, 18), (11, 18), (12, 18)]
    for rects in ([[5, 13, 8, 4], [5, 18, 8, 2]],         # one-tile gap between two parts
                  [[5, 13, 2, 2], [7, 15, 2, 2]]):        # parts touching only diagonally
        with pytest.raises(EditorError) as e:
            tg.classify_rim(tg.rect_tiles(rects))
        assert e.value.code == 'UNSUPPORTED_TERRAIN'
    with pytest.raises(EditorError) as e:                 # stair across a corner
        tg.shaped_terrace_layout(ta.normalise({**LOWER, 'access': [{'side': 'south', 'x': 4, 'z': 19}]}))
    assert e.value.code == 'UNSUPPORTED_ACCESS'
    for rects in ([[10, 10, 6, 1]], [[10, 10, 2, 2], [12, 12, 2, 2]]):   # 1-wide channel; diagonal touch
        with pytest.raises(EditorError) as e:
            tg.shaped_water_layout({'rects': rects})
        assert e.value.code == 'UNSUPPORTED_WATER'
    with pytest.raises(EditorError) as e:                 # disconnected rectangles
        ta.normalise({**RIVER, 'rects': [[0, 0, 2, 2], [5, 5, 2, 2]]})
    assert e.value.code == 'UNSUPPORTED_TERRAIN'
    assert tg.rectangles(tg.rect_tiles(LOWER['rects'])) == [(5, 13, 13, 17), (5, 17, 9, 19)]


def test_two_level_concave_terrace_with_ledge(tmp_path):
    p, ridge = ridge_project(tmp_path)
    plan = p.plan_area_edit([elevation(ridge, LOWER), elevation(ridge, UPPER)])
    reports = [t['report'] for t in plan['transactions'] if t.get('schema') == ta.SHAPE_SCHEMA]
    lower, upper = reports
    assert (lower['ground_height'], lower['top_height'], lower['inner_corners'], lower['outer_corners']) == (64, 80, 1, 5)
    assert lower['ledges'][0]['jump'] == 0x3B and lower['ledges_reachable'] and lower['top_reachable']
    assert (upper['level'], upper['ground_height'], upper['top_height'], upper['base']) == (2, 80, 96, 'terrace_shape@5,13')
    assert upper['top_reachable']
    p.apply_area_edit(66, operations=[elevation(ridge, LOWER), elevation(ridge, UPPER)], label='ridge terraces')
    state = p.composed()
    ctx = p.context(header=ridge, cell=[0, 0])
    h = lambda x, z: ta.tile_height(p, ctx, x, z)
    assert (h(4, 9), h(6, 18), h(8, 14), h(15, 20)) == (4.0, 5.0, 6.0, 4.0)
    assert h(6, 19) == pytest.approx(4.5, abs=0.01) and h(8, 15) == pytest.approx(5.5, abs=0.01)   # stock 2896 normals
    pair = lambda x, z: ta.pair_at(p, state, ctx, x, z)[0]
    assert pair(11, 17) == bytes((0x3B, 0x80)) and pair(9, 17) == tg.PAIRS['rim'] and pair(6, 19) == tg.PAIRS['stair']
    assert pair(8, 14) == tg.PAIRS['ground'] and pair(6, 14) == tg.PAIRS['rim']
    walk = ta.reachable(p, state, ridge, ledges=True)['foot']
    assert {(6, 18), (8, 14), (11, 18)} <= walk
    # One-way: from the ledge landing the lower ridge is not reachable except via the stairs
    # (the flood from the landing alone, ignoring warps, never climbs the ledge).
    assert (11, 16) in walk and (11, 17) not in walk
    # The model holds both levels' stock pieces; the inner corner is two mitred wall halves.
    from sovereign_editor import surface_authoring, mapscene, nitro
    model = surface_authoring.model(p, ctx, state)
    _, blobs = mapscene.tilesets(p, ctx)
    _, prims = nitro.decode_model(model, tileset=blobs['map_tileset'], render=False)
    ys = sorted({round(float(v[1])) for q in prims if q.material['texture_name'] == 'grass01gs' for v in q.vertices})
    assert {80, 96} <= set(ys)
    # Undo removes both levels in one step; redo restores them.
    p.undo(67)
    assert not ta.area_features(p, p.composed(), ridge)
    p.redo(68)
    assert [f['id'] for f in ta.area_features(p, p.composed(), ridge)] == ['terrace_shape@5,13', 'terrace_shape@7,14']


def test_level_two_needs_a_base_and_ledges_need_ground(tmp_path):
    p, ridge = ridge_project(tmp_path)
    with pytest.raises(EditorError) as e:                                                   # no level-1 base yet
        p.plan_area_edit([elevation(ridge, UPPER)])
    assert e.value.code == 'UNSUPPORTED_TERRAIN', str(e.value)
    # A ledge whose landing is not walkable ground refuses (column 17 is a fence).
    bad = {**LOWER, 'rects': [[12, 13, 4, 4]], 'access': [], 'ledges': [{'side': 'east', 'x': 16, 'z': 13, 'length': 3}]}
    with pytest.raises(EditorError) as e:
        p.plan_area_edit([elevation(ridge, bad)])
    assert e.value.code in ('UNSUPPORTED_ACCESS', 'UNSUPPORTED_TERRAIN'), str(e.value)


def test_concave_water_shore_in_canopy(tmp_path):
    p = fresh(tmp_path)
    plan = p.plan_area_edit([elevation(542, RIVER)])
    report = next(t['report'] for t in plan['transactions'] if t.get('schema') == ta.SHAPE_SCHEMA)
    assert report['concave_corner_ends'] == 2 and report['convex_corner_ends'] >= 10
    assert report['surf_reachable'] and report['surf_entries'] > 20
    p.apply_area_edit(64, operations=[elevation(542, RIVER)], label='bend')
    state = p.composed()
    ctx = p.context(header=542, cell=[3, 2])
    assert ta.tile_height(p, ctx, 108, 80) == 0.5 and ta.tile_height(p, ctx, 106, 81) == 1.0
    assert ta.pair_at(p, state, ctx, 103, 83)[0] == tg.PAIRS['water']
    # The shore strip covers the concave corner's water quadrant exactly once on each side of the mitre.
    lib = tg.library(p)
    pieces = tg.shaped_water_pieces(lib, ta.normalise(RIVER), 16)
    # Concave vertex (107, 82): the north-shore run of (106,82) and the west-shore run of (107,81)
    # meet on the diagonal pz - 82 = px - 107; each keeps its own side, both are present.
    near = [poly for _, poly in pieces['clipped']
            if abs(poly[:, 0].mean() - 107) < 0.8 and abs(poly[:, 2].mean() - 82) < 0.8]
    sides = {np.sign(round(float((poly[:, 2] - 82).mean() - (poly[:, 0] - 107).mean()), 6)) for poly in near}
    assert {-1.0, 1.0} <= sides
    for poly in near:
        d = (poly[:, 2] - 82) - (poly[:, 0] - 107)
        assert np.all(d >= -1e-6) or np.all(d <= 1e-6)          # no face crosses the mitre
