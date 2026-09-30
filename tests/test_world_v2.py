"""PROD-VIS-001 world areas v2: 4x4 templates, stock altitudes, texture-level
donor compatibility and a cross-cell dependency review before apply."""
import copy
from pathlib import Path

import pytest

from sovereign_editor import world, world_authoring as wa, snapshots
from sovereign_editor.core import Project, atomic_json
from sovereign_editor.formats import EditorError

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / 'projects/scyther-orchestration-1/baseline.nds'
WINDOW = (39, 11)          # stock matrix 0: Route 15/14/13 cells and filler forest/sea


@pytest.fixture(scope='module')
def workspace(tmp_path_factory):
    if not BASELINE.exists():
        pytest.skip('Pinned baseline is local and untracked')
    root = tmp_path_factory.mktemp('world-v2')
    snapshots.configure(root / 'snapshots')
    p = Project.create(BASELINE, root / 'project')
    return p.root, copy.deepcopy(p.doc)


@pytest.fixture
def p(workspace):
    root, doc = workspace
    atomic_json(root / 'project.json', copy.deepcopy(doc))
    return Project(root)


def window_cells(p, width=4, height=4, origin=WINDOW):
    grid = p.matrix_data(0)
    cells = []
    for y in range(height):
        for x in range(width):
            sx, sy = origin[0] + x, origin[1] + y
            cells.append({'cell': [x, y], 'source': {'header': grid['headers'][sy][sx], 'cell': [sx, sy]}})
    return cells


def canopy(p, cells=None, **extra):
    request = {'action': 'create', 'identity': 'canopy_walk', 'name': 'Canopy Walk', 'internal_name': 'CANOPY_WALK',
               'template_header': 23, 'encounters': 'template', 'worldmap': None, 'close': False,
               'cells': cells or window_cells(p), 'static': 'auto'}
    request.update(extra)
    first = request['cells'][0]['source']
    return {'kind': 'world', 'context': first, 'request': request}


def test_v2_copies_a_stock_window_with_its_altitudes(p):
    plan = p.plan_area_edit([canopy(p)])
    t = plan['transactions'][0]
    assert t['schema'] == wa.SCHEMA_V3 and t['size'] == [4, 4] and len(t['allocation']['maps']) == 16
    grid = p.matrix_data(0)
    trial = p.area_preview_project(plan)
    created = trial.matrix_data(t['allocation']['matrix'])
    assert created['has_altitudes'] and not created['has_headers']
    for cell in t['cells']:
        x, y = cell['cell']
        stock = grid['altitudes'][WINDOW[1] + y][WINDOW[0] + x]
        assert created['altitudes'][y][x] == stock == cell['altitude']
    assert trial.context(header=t['header']['id'], cell=[1, 2])['cell']['altitude'] == 0


def test_v2_refuses_textures_missing_from_the_template_tileset(p):
    cells = window_cells(p, 1, 1)
    cells[0]['source'] = {'header': 72, 'cell': [0, 0]}             # indoor room into an outdoor template
    with pytest.raises(EditorError) as exc:
        p.plan_area_edit([canopy(p, cells)])
    assert exc.value.code == 'INCOMPATIBLE_ASSETS'


def test_v2_bounds_are_a_4x4_rectangle(p):
    cells = window_cells(p, 5, 1)
    with pytest.raises(EditorError):
        p.plan_area_edit([canopy(p, cells)])


def test_review_keeps_internal_dependencies_and_lists_boundary_losses(p):
    review = wa.review(p, canopy(p)['request'])
    assert review['internal_missing'] == []
    assert review['foreign_overhangs'] == []
    assert all(r['position'][0] in (0, 3) or r['position'][1] in (0, 3) for r in review['boundary_losses'])
    grid = p.matrix_data(0)
    altered = window_cells(p)
    altered[2 * 4 + 3]['source'] = {'header': 0, 'cell': [41, 12]}   # filler where Route 14 (168) stood
    review = wa.review(p, canopy(p, altered)['request'])
    assert any(r['position'] == [2, 2] and r['from'] == [3, 2] for r in review['internal_missing'])
    assert any(r['position'] == [2, 2] for r in review['foreign_overhangs'])


def test_created_header_resolves_right_after_apply(p):
    p.apply_area_edit(p.doc['revision'], operations=[canopy(p)])
    # No explicit composed(): header reads must compose v2 areas lazily too.
    assert p.header(540)['name'] == 'CANOPY_WALK' and p.header_count() == 541


def test_v3_refuses_forest_filler_without_static_and_records_static_cells(p):
    """PROD-VIS-002: a copy of stock forest filler 208 would take the area's index-bound water
    animation; creation refuses it unless the cell is declared static."""
    with pytest.raises(EditorError) as exc:
        p.plan_area_edit([canopy(p, static=None)])
    assert exc.value.code == 'INCOMPATIBLE_ANIMATION'
    plan = p.plan_area_edit([canopy(p)])
    t = plan['transactions'][0]
    static = [c for c in t['cells'] if c['static']]
    assert static and all(c['mismatched_slots'] for c in static)
    assert all(not c['mismatched_slots'] for c in t['cells'] if not c['static'])
    trial = p.area_preview_project(plan)
    state = trial.composed()
    assert state['world']['static']['canopy_walk'] == sorted(c['map_member'] for c in static)
    runtime = wa.runtime_plan(trial, state)
    assert runtime['world_static']['members'] == sorted(c['map_member'] for c in static)
    # An explicit list must cover every mismatched cell.
    partial = [static[0]['cell']] if len(static) > 1 else []
    with pytest.raises(EditorError) as exc:
        p.plan_area_edit([canopy(p, static=partial)])
    assert exc.value.code == 'INCOMPATIBLE_ANIMATION'
    with pytest.raises(EditorError):
        p.plan_area_edit([canopy(p, static=[[9, 9]])])
