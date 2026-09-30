"""Terrain authoring v1: terraces, stairs, ponds, seams and the WORLD-AUDIO-001 sound plates.

Runs on private copies of the accepted r56 project (projects/world-integration-v1 is only
copied, never opened for writing). Heights are cross-checked against the real overlay-1
height routine in Unicorn (tools/terrain_readback.py). Software evidence only; native
traversal and audio are the user-run checklist.
"""
import copy
import hashlib
import json
import shutil
import sys
from pathlib import Path

import numpy as np
import pytest

from sovereign_editor import terrain_authoring as ta, terrain_geometry as tg, scenery
from sovereign_editor.core import Project, atomic_json
from sovereign_editor.formats import EditorError, map_sections, member_count, resource

ROOT = Path(__file__).resolve().parents[1]
PARENT = ROOT / 'projects/world-integration-v1'
R56 = PARENT / 'exports/world-integration-r56/game.nds'
R56_SHA = '2bf7cbbb76e85e043d844e3b46046bb43f5deef04389664ad4910a9e3ae418f2'
R56_WITH_ARRIVAL_FIX_SHA = '22a6fa13322d8268a6a509e88c8061689a75874e0afaa2dbdc2bf214a93d8d6e'
# Unicorn: a system install in cloud; on the Mac it lives in untracked work/tiana-fixes-1/python-tools.
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'work/tiana-fixes-1/python-tools')]
import terrain_readback  # noqa: E402

CELL = {'header': 542, 'cell': [3, 2]}
TERRACE = {'action': 'terrace', 'x': 98, 'z': 63, 'width': 4, 'height': 8,
           'access': [{'side': 'north', 'offset': 0}, {'side': 'south', 'offset': 0}], 'label': 'Seam terrace'}
POND = {'action': 'pond', 'x': 101, 'z': 77, 'width': 4, 'height': 3, 'traversable': True, 'label': 'Pond'}


def op(request, context=CELL):
    return {'kind': 'elevation', 'context': context, 'request': request}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.fixture(scope='module')
def parent_copy(tmp_path_factory):
    root = tmp_path_factory.mktemp('terrain') / 'parent'
    shutil.copytree(PARENT, root, ignore=shutil.ignore_patterns('exports'))
    return root


@pytest.fixture
def project(parent_copy, tmp_path):
    return Project(parent_copy).clone(tmp_path / 'p')


@pytest.fixture(scope='module')
def applied(parent_copy, tmp_path_factory):
    root = tmp_path_factory.mktemp('applied') / 'p'
    p = Project(parent_copy).clone(root)
    p.apply_area_edit(p.doc['revision'], operations=[op(TERRACE), op(POND),
                      op({'action': 'ambient', 'cell': [3, 1], 'remove': list(range(20))}),
                      op({'action': 'ambient', 'cell': [3, 2], 'remove': list(range(8))})], label='fixture')
    return root


def test_donor_is_pinned_and_complete(project):
    lib = tg.library(project)
    assert len(lib['wall'].polys) == 3 and len(lib['corner'].polys) == 11 and len(lib['stair'].polys) == 19
    assert np.allclose(lib['wall'].grad['wall01_g'], [0.5, 0]) and np.allclose(lib['shore_grad']['pond_line'], [-1, 0])
    raw = project.member_raw(tg.DONOR['member'])
    assert hashlib.sha256(raw).hexdigest() == tg.DONOR_SHA256
    tampered = bytearray(raw)
    tampered[-1] ^= 1
    with pytest.raises(EditorError) as exc:
        tg._library(bytes(tampered), b'')
    assert exc.value.code == 'UNQUALIFIED_DONOR'


def test_quarter_turns_are_proper_rotations():
    poly = np.zeros((3, 10))
    poly[:, 0], poly[:, 2], poly[:, 9] = [0.2, 0.7, 0.4], [0.1, 0.3, 0.9], 0x21
    poly[:, 8] = tg.pack_normal(100, 300, -200)
    area = tg.area_xz(poly)
    for k in range(1, 4):
        turned = tg.turn(poly, k)
        assert np.isclose(tg.area_xz(turned), area)          # winding preserved (no mirror)
    assert np.allclose(tg.turn(poly, 4), poly)
    assert tg.unpack_normal(tg.turn(poly, 1)[0, 8]) == [200, 300, 100]


def test_bdhc_writer_matches_stock_layout_and_runtime_heights():
    blob = (PARENT / 'baseline.nds').read_bytes()
    cpu = terrain_readback.Heights(blob)
    for member in (168, 164):
        raw = resource(blob, 'a/0/6/5', member)[1]
        data = raw[map_sections(raw)['terrain_offset']:]
        table = tg.parse_bdhc(data)
        rebuilt = tg.build_bdhc(table['plates'])
        if member == 168:
            assert rebuilt == data                          # stock layout reproduced byte-exactly
        runtime = cpu.grid(data)
        for (tx, tz), h in runtime.items():
            assert tg.height_at(table, tx - 15.5, tz - 15.5) == pytest.approx(h, abs=1 / 4096) if h is not None \
                else tg.height_at(table, tx - 15.5, tz - 15.5) is None
        again = cpu.grid(rebuilt)
        assert again == runtime                              # rebuilt table answers identically
    # Stair constants use the exact unit normal; the stock tool's own values differ by at most
    # one fx32 LSB (its float precision varies per stair), far below any runtime effect.
    def stock(member, index):
        raw = resource(blob, 'a/0/6/5', member)[1]
        return tg.parse_bdhc(raw[map_sections(raw)['terrain_offset']:])['plates'][index]
    for side, edge, member, index in (('south', (0, -5), 168, 5), ('north', (0, -15), 164, 0), ('west', (-8, 0), 164, 6)):
        mine, theirs = tg.slope(side, edge, 2.0), stock(member, index)
        assert mine['normal'] == theirs['normal'] and abs(mine['d'] - theirs['d']) <= 2


def test_preview_reports_family_seam_and_access(project):
    plan = project.plan_area_edit([op(TERRACE)])
    kinds = [t['schema'] for t in plan['transactions']]
    assert kinds.count(ta.SCHEMA) == 2 and len(kinds) == 4
    report = plan['preview'][0]['report']
    assert report['family'] == ta.FAMILY and report['ground_height'] == 16 and report['top_height'] == 32
    assert report['seams'] == ['z=64'] and report['top_reachable'] and report['rim_tiles'] == 22
    assert [s['side'] for s in report['stairs']] == ['north', 'south']
    cells = {(c['x'], c['z']): c['after'] for t in plan['preview'] if t['operation'] == 'map.transaction'
             for c in t['permission_cells']}
    assert cells[(99, 62)] == '0006' and cells[(97, 64)] == '0080' and cells[(99, 64)] == '0004'
    assert project.doc['revision'] == 56 and len(project.doc['map_edits']) == 534


def test_apply_undo_redo_reopen_and_floor_height(applied):
    p = Project(applied)
    assert p.doc['revision'] == 57 and len(p.doc['map_edits']) == 542 and len(p.doc['history']) == 39
    ctx = p.context(header=542, cell=[3, 2])
    assert scenery.floor_height(p, ctx, {'x': 99.5, 'z': 66.5}) == 2.0
    assert scenery.floor_height(p, ctx, {'x': 102.5, 'z': 78.5}) == 0.5
    view = p.terrain_view(542)
    assert view['rain_plate_conflicts'] == 0 and len(view['features']) == 2
    scratch = applied.parent / 'undo'
    shutil.copytree(applied, scratch)
    q = Project(scratch)
    q.undo(q.doc['revision'])
    assert len(q.doc['map_edits']) == 534 and not q.composed().get('terrain_features')
    assert q.terrain_view(542)['rain_plate_conflicts'] == 28
    q.redo(q.doc['revision'])
    assert q.doc['map_edits'] == p.doc['map_edits']
    assert Project(scratch).terrain_view(542)['features'] == view['features']


def test_export_readback_and_determinism(applied, tmp_path):
    p = Project(applied)
    a = p.export(tmp_path / 'a', p.doc['revision'])
    b = Project(applied).export(tmp_path / 'b', p.doc['revision'])
    assert a['candidate_sha256'] == b['candidate_sha256']
    if not R56.exists():
        # The approved 2026-09-28 storage cleanup removed old generated exports (receipt:
        # evidence/storage-audit-2026-09-28/deletion-receipt.json); they are not recreated.
        pytest.skip('historical r56 export removed by the storage cleanup; determinism was checked')
    rom = (tmp_path / 'a/game.nds').read_bytes()
    ref = R56.read_bytes()
    assert sha(R56) == R56_SHA
    changed = [i for i in range(member_count(rom, 'a/0/6/5'))
               if resource(rom, 'a/0/6/5', i)[1] != resource(ref, 'a/0/6/5', i)[1]]
    assert changed == [687, 691]
    for m in changed:
        raw = resource(rom, 'a/0/6/5', m)[1]
        sec = map_sections(raw)
        assert sec['bgs_bytes'] == 4 and raw[sec['terrain_offset']:] == p._terrain_bdhc[m]
    spec_path = tmp_path / 'spec.json'
    import subprocess
    subprocess.run([sys.executable, str(ROOT / 'work/terrain-authoring-v1/scripts/readback_spec.py'), str(applied),
                    str(spec_path)], check=True, capture_output=True)
    report = terrain_readback.main(str(tmp_path / 'a/game.nds'), str(R56), str(spec_path))
    assert not report['failures'] and len(report['checks']) >= 21


@pytest.mark.parametrize('request_, code', [
    ({**TERRACE, 'z': 60}, 'UNSUPPORTED_ACCESS'),                              # covers stock stair landing
    ({**TERRACE, 'x': 98, 'z': 41, 'height': 2, 'access': []}, 'STRANDED'),        # cuts off the fenced nook
    ({**TERRACE, 'z': 62, 'height': 7, 'access': [{'side': 'west', 'offset': 0}, {'side': 'north', 'offset': 0},
                                                  {'side': 'south', 'offset': 0}]}, 'UNSUPPORTED_ACCESS'),  # stair straddles seam
    ({**TERRACE, 'x': 70, 'z': 68, 'access': [{'side': 'south', 'offset': 0}]}, 'UNSUPPORTED_TERRAIN'),  # no donor materials
    ({**TERRACE, 'x': 104, 'z': 40, 'access': []}, 'UNSUPPORTED_TERRAIN'),      # trees/walkway, not ground
    ({**TERRACE, 'width': 13}, 'UNSUPPORTED_TERRAIN'),                          # measured side limit
    ({**TERRACE, 'access': [{'side': 'north', 'offset': 2}]}, 'UNSUPPORTED_ACCESS'),
    ({**TERRACE, 'x': 124, 'z': 76}, 'UNSUPPORTED_TERRAIN'),                    # sea / outside ground
    ({**POND, 'x': 108, 'z': 76}, 'UNSUPPORTED_TERRAIN'),                       # fence tiles in footprint
    ({**POND, 'width': 1}, 'UNSUPPORTED_TERRAIN'),
    ({'action': 'ambient', 'cell': [3, 2], 'remove': [8]}, 'UNSUPPORTED_AMBIENT'),
    ({'action': 'ambient', 'cell': [3, 2], 'remove': [1, 1]}, 'INVALID_INPUT'),
    ({'action': 'lift'}, 'INVALID_INPUT'),
])
def test_refusals_write_nothing(project, request_, code):
    before = copy.deepcopy(project.doc)
    with pytest.raises(EditorError) as exc:
        project.plan_area_edit([op(request_)])
    assert exc.value.code == code, str(exc.value)
    assert project.doc == before


def test_overlap_stale_and_protection(applied, tmp_path):
    root = tmp_path / 'p'
    shutil.copytree(applied, root)
    p = Project(root)
    revision = p.doc['revision']
    with pytest.raises(EditorError) as exc:
        p.plan_area_edit([op({**POND, 'x': 102})])
    assert exc.value.code == 'UNSUPPORTED_TERRAIN'
    with pytest.raises(EditorError) as exc:
        p.apply_area_edit(revision - 1, operations=[op({**POND, 'x': 97, 'z': 80})])
    assert exc.value.code == 'STALE_REVISION'
    with pytest.raises(EditorError) as exc:
        p.plan_area_edit([{'kind': 'terrain', 'context': CELL,
                           'request': {'x': 97, 'z': 65, 'width': 1, 'height': 1, 'blocked': False}}])
    assert exc.value.code == 'TERRAIN_OWNED'
    with pytest.raises(EditorError) as exc:
        p.plan_area_edit([{'kind': 'surface', 'context': CELL,
                           'request': {'tiles': [{'x': 99, 'z': 66}], 'material': 'road01'}}])
    assert exc.value.code in ('TERRAIN_OWNED', 'UNSUPPORTED_SURFACE')
    assert p.doc['revision'] == revision


@pytest.mark.parametrize('field, value', [('model_after_sha256', '0' * 64), ('ground', 17), ('report', {}),
                                          ('request', {**TERRACE, 'x': 99})])
def test_tampered_terrain_transaction_refuses_to_open(applied, tmp_path, field, value):
    root = tmp_path / 'p'
    shutil.copytree(applied, root)
    doc = json.loads((root / 'project.json').read_text())
    target = next(t for t in doc['map_edits'] if t.get('schema') == ta.SCHEMA)
    target[field] = value
    atomic_json(root / 'project.json', doc)
    with pytest.raises(EditorError):
        Project(root)


def test_tampered_ambient_before_value_refuses(applied, tmp_path):
    root = tmp_path / 'p'
    shutil.copytree(applied, root)
    doc = json.loads((root / 'project.json').read_text())
    target = next(t for t in doc['map_edits'] if t.get('schema') == ta.AMBIENT_SCHEMA)
    target['before'] = target['before'][1:]
    atomic_json(root / 'project.json', doc)
    with pytest.raises(EditorError):
        Project(root)


def test_accepted_parent_replays_and_reexports_unchanged(parent_copy, tmp_path):
    p = Project(parent_copy)
    assert p.doc['revision'] == 56 and len(p.doc['map_edits']) == 534 and len(p.doc['history']) == 38
    assert not p.composed().get('terrain_features') and not p._terrain_bdhc
    # r56 has authored connections, so today's export includes the R82-WARP-01 arrival fix (two
    # overlay-1 calls + two resident routines). Without that one fix the bytes are the accepted r56.
    from sovereign_editor import warp_arrival
    fixed = p.export(tmp_path / 'r56-fixed', 56)
    assert fixed['candidate_sha256'] == R56_WITH_ARRIVAL_FIX_SHA
    original = warp_arrival.bindings
    warp_arrival.bindings = lambda blob, plan, layout: plan
    try:
        result = p.export(tmp_path / 'r56', 56)
    finally:
        warp_arrival.bindings = original
    assert result['candidate_sha256'] == R56_SHA


def test_cli_inspect_and_area_edit_share_the_operation(project, tmp_path):
    import subprocess
    run = lambda *a: json.loads(subprocess.run([sys.executable, '-m', 'sovereign_editor.cli', *a], check=True,
                                               capture_output=True, text=True, cwd=ROOT).stdout)['result']
    view = run('terrain-inspect', '--project', str(project.root), '--header', '542')
    assert view['family'] == ta.FAMILY and view['rain_plate_conflicts'] == 28
    assert [p['sound'] for p in view['cells'][-1]['sound_plates']][:1] == ['seashore']
    request = tmp_path / 'r.json'
    request.write_text(json.dumps({'operations': [op(TERRACE)], 'label': 'cli'}))
    dry = run('area-edit', '--project', str(project.root), '--request', str(request), '--dry-run')
    assert dry['revision'] == 56 and dry['preview'][0]['operation'] == 'terrain.feature'
    done = run('area-edit', '--project', str(project.root), '--request', str(request), '--revision', '56')
    assert done['revision'] == 57 and done['changed']
    assert len(Project(project.root).doc['map_edits']) == 538      # 2 terrain + 2 permission transactions


def test_native_elevation_tab_previews_and_applies(project):
    from PySide6.QtWidgets import QApplication, QWidget
    from sovereign_editor import world_ui
    app = QApplication.instance() or QApplication([])
    inspector = QWidget()
    inspector.project, inspector.header, inspector.cell = project, 542, [3, 2]
    editor = world_ui.WorldEditor(inspector)
    editor.inspect_elevation()
    assert 'rain conflicts 28' in editor.summary.toPlainText()
    editor.el_action.setCurrentIndex(2)
    editor.el_cell.setText('3,2')
    editor.stage_elevation()
    assert 'seashore@[109, 75]' in editor.summary.toPlainText() and editor.apply_button.isEnabled()
    editor.apply()
    assert project.doc['revision'] == 57 and project.terrain_view(542)['rain_plate_conflicts'] == 20
    editor.el_action.setCurrentIndex(1)
    for widget, value in ((editor.el_x, 101), (editor.el_z, 77), (editor.el_w, 4), (editor.el_h, 3)):
        widget.setValue(value)
    editor.stage_elevation()
    assert 'Surf water' in editor.summary.toPlainText()
    editor.apply()
    assert project.doc['revision'] == 58
    inspector.deleteLater()
