"""PROD-VIS-001: bordered path and tall-grass patches across a created-area seam."""
import copy
from pathlib import Path

import numpy as np
import pytest

from sovereign_editor import borders, snapshots, surface_authoring as sa, world
from sovereign_editor.core import Project, atomic_json
from sovereign_editor.formats import EditorError, map_data

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / 'projects/scyther-orchestration-1/baseline.nds'
WEST, EAST = [1, 2], [2, 2]              # Route 15 maps 166 | 167 inside the 4x4 window
# Verified open ground in Route 15 maps 166|167: the path avoids raised scenery at both
# ends, the grass avoids the stock road (and its road01_sub transitions) to the north.
PATH = [{'x': x, 'z': 83} for x in range(56, 72)]
GRASS = [{'x': x, 'z': 71} for x in range(59, 64)] + [{'x': x, 'z': 72} for x in range(59, 68)]


def window(p):
    grid = p.matrix_data(0)
    return [{'cell': [x, y], 'source': {'header': grid['headers'][11 + y][39 + x], 'cell': [39 + x, 11 + y]}}
            for y in range(4) for x in range(4)]


@pytest.fixture(scope='module')
def workspace(tmp_path_factory):
    if not BASELINE.exists():
        pytest.skip('Pinned baseline is local and untracked')
    root = tmp_path_factory.mktemp('borders')
    snapshots.configure(root / 'snapshots')
    p = Project.create(BASELINE, root / 'project')
    cells = window(p)
    p.apply_area_edit(0, operations=[{'kind': 'world', 'context': cells[0]['source'], 'request': {
        'action': 'create', 'identity': 'canopy_walk', 'name': 'Canopy Walk', 'internal_name': 'CANOPY_WALK',
        'template_header': 23, 'encounters': 'template', 'worldmap': None, 'close': False, 'cells': cells,
        'static': 'auto'}}])
    return p.root, copy.deepcopy(p.doc)


@pytest.fixture
def p(workspace):
    root, doc = workspace
    atomic_json(root / 'project.json', copy.deepcopy(doc))
    return Project(root)


def border(family, tiles, cell=WEST):
    return {'kind': 'border', 'context': {'header': 540, 'cell': cell},
            'request': {'family': family, 'tiles': tiles, 'label': f'{family} border'}}


def pair(p, header, x, z):
    ctx = p.context(header=header, cell=[x // 32, z // 32])
    at = world.cell_offset(ctx, x, z)
    member = ctx['map_member']
    return p.composed()['permissions'].get((member, at), p.member_raw(member)[at:at + 2])


def test_bordered_path_crosses_the_seam_with_stock_rims(p):
    plan = p.plan_area_edit([border('path', PATH)])
    surfaces = [t for t in plan['transactions'] if t['schema'] == sa.BORDER_SCHEMA]
    assert [t['context']['cell'] for t in surfaces] == [WEST, EAST]
    trial = p.area_preview_project(plan)
    state = trial.composed()
    west = trial.context(header=540, cell=WEST)
    east = trial.context(header=540, cell=EAST)
    wcells = state['surfaces'][west['map_member']]
    ecells = state['surfaces'][east['map_member']]
    assert wcells[(24, 19)]['material'] == ecells[(7, 19)]['material']              # path both sides
    assert 'affine' in wcells[(31, 18)] and 'affine' in ecells[(0, 18)]             # rim continues over the seam
    assert 'affine' in wcells[(23, 19)] and 'affine' in ecells[(8, 19)]             # end caps
    for x in (56, 63, 64, 71):
        b = pair(trial, 540, x, 83)
        assert b[0] == 0 and not b[1] & 0x80
    for ctx in (west, east):
        from sovereign_editor import nitro, mapscene
        nitro.decode_model(sa.model(trial, ctx, state), tileset=mapscene.tilesets(trial, ctx)[1]['map_tileset'])


def test_tall_grass_crosses_the_seam_with_stock_pieces(p):
    plan = p.plan_area_edit([border('tall_grass', GRASS)])
    trial = p.area_preview_project(plan)
    state = trial.composed()
    west = trial.context(header=540, cell=WEST)
    east = trial.context(header=540, cell=EAST)
    wp = state['surface_pieces'][west['map_member']]
    ep = state['surface_pieces'][east['map_member']]
    kinds = {v['material'] for v in wp.values()} | {v['material'] for v in ep.values()}
    assert kinds == set(borders.GRASS_FAMILY)                                     # L-shape: inner corner too
    # Bottom edge strip continues across the seam: last west half-tile and first east one.
    assert any(v['material'] == 'egrass_u' and v['rect'][2] == 256 for v in wp.values())
    assert any(v['material'] == 'egrass_u' and v['rect'][0] == -256 for v in ep.values())
    for x, z in ((59, 71), (63, 72), (64, 72), (67, 72)):
        b = pair(trial, 540, x, z)
        assert b[0] == 0x02 and not b[1] & 0x80
    assert pair(trial, 540, 68, 72)[0] == 0x00 and pair(trial, 540, 64, 71)[0] == 0x00   # ring tiles unchanged


def test_one_tile_gap_is_refused_before_write(p):
    before = p.path.read_bytes()
    with pytest.raises(EditorError) as exc:
        p.plan_area_edit([border('path', [{'x': 52, 'z': 83}, {'x': 54, 'z': 83}])])
    assert exc.value.code == 'UNSUPPORTED_BORDER' and p.path.read_bytes() == before


def test_pieces_over_raised_or_missing_ground_are_refused(p):
    with pytest.raises(EditorError) as exc:
        p.plan_area_edit([border('tall_grass', [{'x': 58, 'z': 77}])])             # row 13: blocked scenery
    assert exc.value.code in ('UNSUPPORTED_BORDER', 'UNSUPPORTED_SURFACE', 'UNSUPPORTED_TERRAIN')


def test_ring_leaving_the_area_is_refused(p):
    with pytest.raises(EditorError) as exc:
        p.plan_area_edit([border('path', [{'x': 0, 'z': 83}], cell=[0, 2])])
    assert exc.value.code == 'UNSUPPORTED_BORDER'


# ---- rerouting stock road inside a window (cut ends keep stock caps) --------------------
TOP_WINDOW = {'x': 42, 'z': 69, 'width': 27, 'height': 5}        # road band rows 5..9 of 166|167
NEW_PATH = ([{'x': x, 'z': 70} for x in range(42, 51)] + [{'x': 50, 'z': 71}]
            + [{'x': x, 'z': 72} for x in range(50, 69)])
BOTTOM_WINDOW = {'x': 48, 'z': 81, 'width': 36, 'height': 4}     # rims + road rows 17..20
NEW_GRASS = [{'x': x, 'z': z} for z in (82, 83) for x in range(58, 70)]


def windowed(family, tiles, window, cell=WEST):
    op = border(family, tiles, cell)
    op['request']['window'] = window
    return op


def entries(p, cell):
    ctx = p.context(header=540, cell=cell)
    ox, oz = ctx['origin']
    return {(x + ox, z + oz): v for (x, z), v in p.composed()['surfaces'][ctx['map_member']].items()}


def texture_of(p, cell, entry):
    ctx = p.context(header=540, cell=cell)
    from sovereign_editor import border_authoring as ba
    names = {material: texture for texture, (material, _) in ba.shapes(p, ctx, p.composed()).items()}
    return names[entry['material']]


def test_window_reroutes_the_stock_road_with_junction_corners(p):
    plan = p.plan_area_edit([windowed('path', NEW_PATH, TOP_WINDOW)])
    trial = p.area_preview_project(plan)
    got = {**entries(trial, WEST), **entries(trial, EAST)}
    tex = lambda t: texture_of(trial, WEST if t[0] < 64 else EAST, got[t])
    for x in range(42, 69):
        for z in range(69, 74):
            assert (x, z) in got                                        # every window tile is decided
    assert all(tex((t['x'], t['z'])) == 'road01' for t in NEW_PATH)
    assert tex((45, 73)) == 'grass01gs' and tex((60, 69)) == 'grass01gs'    # old rims/road cleared
    assert tex((42, 71)) == 'road01_r' and tex((49, 71)) == 'road01_r'      # junction and turn corners
    assert tex((64, 71)) == 'road01_r' and tex((63, 73)) == 'road01_r'      # rims continue over the seam
    assert pair(trial, 540, 50, 71)[0] == 0 and not pair(trial, 540, 50, 71)[1] & 0x80


def test_window_clears_road_then_tall_grass_crosses_the_seam(p):
    plan = p.plan_area_edit([windowed('path', [], BOTTOM_WINDOW), border('tall_grass', NEW_GRASS)])
    trial = p.area_preview_project(plan)
    got = {**entries(trial, WEST), **entries(trial, EAST)}
    tex = lambda t: texture_of(trial, WEST if t[0] < 64 else EAST, got[t])
    assert tex((60, 82)) == 'grass01gs' and tex((66, 83)) == 'grass01gs'
    assert tex((56, 81)) == 'grass01gs' and tex((66, 81)) == 'grass01gs'   # rims under the stock grass border too
    assert tex((47, 82)) if (47, 82) in got else True
    for x, z in ((58, 82), (63, 83), (64, 82), (69, 83)):
        b = pair(trial, 540, x, z)
        assert b[0] == 0x02 and not b[1] & 0x80
    state = trial.composed()
    west = trial.context(header=540, cell=WEST)
    assert any(v['material'] == 'egrass_u' for v in state['surface_pieces'][west['map_member']].values())


def test_decorative_tall_grass_keeps_ordinary_walkable_ground(p):
    """KIT-01 CG-07: the stock tall-grass look with ordinary ground behavior (no encounters), so the
    art stays separate from encounter rules; behavior applies to tall grass only."""
    request = border('tall_grass', GRASS)
    request['request']['behavior'] = 'ground'
    plan = p.plan_area_edit([request])
    trial = p.area_preview_project(plan)
    state = trial.composed()
    west = trial.context(header=540, cell=WEST)
    assert {v['material'] for v in state['surface_pieces'][west['map_member']].values()} <= set(borders.GRASS_FAMILY)
    for x, z in ((59, 71), (63, 72), (64, 72), (67, 72)):
        b = pair(trial, 540, x, z)
        assert b[0] == 0x00 and not b[1] & 0x80
    for bad in ({'family': 'path', 'behavior': 'ground'}, {'family': 'tall_grass', 'behavior': 'hedge'}):
        wrong = border(bad['family'], GRASS)
        wrong['request']['behavior'] = bad['behavior']
        with pytest.raises(EditorError):
            p.plan_area_edit([wrong])
