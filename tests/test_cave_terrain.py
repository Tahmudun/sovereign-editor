"""Cave family cave-d41-v1 (TERRAIN-03): carved rooms, corridors, walls, exits, encounters and
same-area hole warps. Software checks only."""
from pathlib import Path

import numpy as np
import pytest

from sovereign_editor import terrain_authoring as ta, cave_geometry as cg, world_authoring as wa, event_authoring as ev
from sovereign_editor.core import Project
from sovereign_editor.formats import EditorError

ROOT = Path(__file__).resolve().parents[1]
PARENT = ROOT / 'projects/assets-gameplay-v1'
pytestmark = pytest.mark.skipif(not PARENT.is_dir(), reason='returned assets-gameplay-v1 (r64) parent absent')

CAVE = {'kind': 'world', 'context': {'header': 461, 'cell': [0, 0]}, 'request': {
    'action': 'create', 'identity': 'ec_cave', 'name': 'Ridge Cave', 'internal_name': 'EC_CAVE',
    'template_header': 461, 'encounters': 'template', 'worldmap': [19, 12],
    'cells': [{'cell': [0, 0], 'source': {'header': 461, 'cell': [0, 0]}}]}}
HUT = {'kind': 'world', 'context': {'header': 72}, 'request': {
    'action': 'create', 'identity': 'ec_hut', 'name': 'Ridge Hut', 'internal_name': 'EC_HUT',
    'template_header': 72, 'encounters': 'none', 'worldmap': [19, 12],
    'cells': [{'cell': [0, 0], 'source': {'header': 72, 'cell': [0, 0]}}]}}
# Room A (9x9), a 2-wide corridor south, room B (13x8) with two south exits.
ROOMS = {'action': 'cave_room', 'rects': [[17, 3, 9, 9], [20, 12, 2, 9], [15, 21, 13, 8]],
         'exits': [{'x': 17, 'z': 28}, {'x': 24, 'z': 28}], 'encounters': True, 'label': 'Ridge cave rooms'}


def cave_project(tmp_path):
    p = Project(PARENT).clone(tmp_path / 'p')
    p.apply_area_edit(64, operations=[CAVE, HUT], label='cave')
    areas = {a['identity']: a['header'] for a in p.world_areas()['areas']}
    return p, areas['ec_cave'], areas['ec_hut']


def carve(header, spec=ROOMS):
    return {'kind': 'elevation', 'context': {'header': header, 'cell': [0, 0]}, 'request': spec}


def link(source, x, z, target, ax, az):
    return {'kind': 'world', 'context': {'header': source, 'cell': [0, 0]},
            'request': {'action': 'connect', 'x': x, 'z': z, 'destination': {'header': target},
                        'arrival': {'x': ax, 'z': az}}}


def test_cave_layout_classification_and_refusals():
    lay = cg.layout(ta.normalise(ROOMS))
    assert len(lay['floor']) == 9 * 9 + 2 * 9 + 13 * 8
    assert [v for v, _ in lay['concave']] == [(20, 12), (20, 21), (22, 12), (22, 21)]
    assert sorted(k for _, k in lay['corners']).count(('north', 'west')) == 2          # A and B north-west
    for rects, code in (([[17, 3, 9, 9], [18, 12, 1, 5]], 'UNSUPPORTED_TERRAIN'),     # one-tile passage
                        ([[17, 3, 9, 9], [17, 14, 9, 4]], 'UNSUPPORTED_TERRAIN'),     # rooms too close
                        ([[17, 3, 9, 9], [25, 5, 5, 2]], 'UNSUPPORTED_TERRAIN')):     # corridor at a corner
        with pytest.raises(EditorError) as e:
            cg.layout({'rects': rects, 'exits': []})
        assert e.value.code == code, (rects, str(e.value))
    with pytest.raises(EditorError) as e:                 # exit needs a straight south run of five
        cg.layout({**ROOMS, 'exits': [{'x': 16, 'z': 28}]})
    assert e.value.code == 'UNSUPPORTED_ACCESS'


def test_carved_cave_rooms_exits_and_same_area_holes(tmp_path):
    p, cave, hut = cave_project(tmp_path)
    plan = p.plan_area_edit([carve(cave)])
    report = next(t['report'] for t in plan['transactions'] if t.get('schema') == ta.SHAPE_SCHEMA)
    assert (report['family'], report['floor_tiles'], report['concave_corners'], report['convex_corners']) == \
        ('cave-d41-v1', 203, 4, 8)
    p.apply_area_edit(65, operations=[carve(cave)], label='carve')
    # Exits: one to the hut, one back to the stock entrance hall's hole (same area).
    p.apply_area_edit(66, operations=[link(cave, 24, 28, hut, 4, 8), link(cave, 17, 28, cave, 5, 8)], label='holes')
    state = p.composed()
    ctx = p.context(header=cave, cell=[0, 0])
    assert ta.tile_height(p, ctx, 21, 16) == 1.0 and ta.tile_height(p, ctx, 21, 30) is None
    pair = lambda x, z: ta.pair_at(p, state, ctx, x, z)[0]
    assert (pair(20, 5), pair(16, 5), pair(24, 28), pair(24, 29)) == (cg.PAIRS['floor'], cg.PAIRS['wall'],
                                                                      cg.PAIRS['exit'], cg.PAIRS['hole'])
    warps = [r for r in ev.records(ev.raw_member(p, ctx['event_member'], state)) if r['kind'] == 'warp']
    by_tile = {(w['x'], w['z']): w for w in warps}
    assert by_tile[(17, 28)]['destination'] == cave and by_tile[(5, 8)]['destination'] == cave
    assert by_tile[(17, 28)]['destination_warp'] == by_tile[(5, 8)]['id']
    assert by_tile[(5, 8)]['destination_warp'] == by_tile[(17, 28)]['id']
    walk = ta.reachable(p, state, cave, ledges=True)['foot']
    assert {(20, 5), (21, 16), (26, 27), (5, 5)} <= walk
    # The model carries every carved piece; walls stand on the floor and reach the rock top.
    from sovereign_editor import surface_authoring, mapscene, nitro
    model = surface_authoring.model(p, ctx, state)
    _, blobs = mapscene.tilesets(p, ctx)
    _, prims = nitro.decode_model(model, tileset=blobs['map_tileset'], render=False)
    heights = {}
    for q in prims:
        name = q.material['texture_name']
        for v in q.vertices:
            heights.setdefault(name, set()).add(round(float(v[1])))
    assert {16, 32, 48} <= heights['dwall_n'] and heights['chole_in'] and heights['droad01'] == {16}
    # Encounters: the created cave owns a private copy of the donor's wild table.
    data = p.gameplay_data(cave)
    assert data['resources']['encounter_users'] == [cave] and data['encounters']['methods']['grass']['rate'] > 0
    p.undo(67)
    p.undo(68)
    assert not ta.area_features(p, p.composed(), cave)


def test_carving_refuses_occupied_space(tmp_path):
    p, cave, hut = cave_project(tmp_path)
    for spec in ({**ROOMS, 'rects': [[5, 3, 4, 4]], 'exits': []},             # the stock hall is not void
                 {**ROOMS, 'rects': [[27, 3, 4, 4]], 'exits': []}):            # walls would leave the cell
        with pytest.raises(EditorError) as e:
            p.plan_area_edit([carve(cave, spec)])
        assert e.value.code == 'UNSUPPORTED_TERRAIN', str(e.value)


def test_elevation_tab_stages_shapes_and_cave_rooms(tmp_path):
    import os
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtWidgets import QApplication, QWidget
    from sovereign_editor import world_ui
    from sovereign_editor.gui import STYLE
    app = QApplication.instance() or QApplication([]); app.setStyleSheet(STYLE)
    p, cave, hut = cave_project(tmp_path)
    inspector = QWidget()
    inspector.project, inspector.header, inspector.cell = p, cave, [0, 0]
    editor = world_ui.WorldEditor(inspector)
    editor.el_action.setCurrentIndex(editor.el_action.findData('cave_room')); app.processEvents()
    assert editor.el_form.isRowVisible(editor.el_exits) and not editor.el_form.isRowVisible(editor.el_x)
    editor.el_rects.setText('17,3,9,9; 20,12,2,9; 15,21,13,8')
    editor.el_exits.setText('17,28; 24,28')
    before = p.path.read_bytes()
    editor.stage_elevation()
    text = editor.summary.toPlainText()
    assert p.path.read_bytes() == before and 'concave 4' in text and 'exits [17, 28], [24, 28]' in text, text
    out = ROOT / 'evidence/editor-completion-v1/ui'; out.mkdir(parents=True, exist_ok=True)
    editor.tabs.setCurrentIndex(next(i for i in range(editor.tabs.count()) if editor.tabs.tabText(i) == 'Elevation'))
    editor.resize(1000, 720); app.processEvents(); assert editor.grab().save(str(out / 'elevation-cave-compact.png'))
    editor.apply()
    assert [f['id'] for f in ta.area_features(p, p.composed(), cave)] == ['cave_room@15,21']
    editor.el_action.setCurrentIndex(editor.el_action.findData('terrace_shape')); app.processEvents()
    assert editor.el_form.isRowVisible(editor.el_ledges) and editor.el_form.isRowVisible(editor.el_level)
    inspector.deleteLater()


def test_move_a_connected_entrance_keeps_both_ends_paired(tmp_path):
    p, cave, hut = cave_project(tmp_path)
    p.apply_area_edit(65, operations=[carve(cave), link(cave, 5, 8, hut, 4, 8)], label='carve and link hall')
    state = p.composed()
    conn = state['world']['connections'][-1]['index']
    move = {'kind': 'world', 'context': {'header': cave, 'cell': [0, 0]},
            'request': {'action': 'move', 'connection': conn, 'source': [{'x': 24, 'z': 28}]}}
    for bad, code in (({'source': [{'x': 20, 'z': 5}]}, 'UNSUPPORTED_ENTRANCE'),        # plain floor
                      ({'source': [{'x': 24, 'z': 28}, {'x': 17, 'z': 28}]}, 'INVALID_INPUT'),
                      ({'connection': 9999}, 'NOT_FOUND')):
        with pytest.raises(EditorError) as e:
            p.plan_area_edit([{**move, 'request': {**move['request'], **bad}}])
        assert e.value.code == code, (bad, str(e.value))
    p.apply_area_edit(66, operations=[move], label='move hall entrance')
    state = p.composed()
    cave_ctx, hut_ctx = p.context(header=cave, cell=[0, 0]), p.context(header=hut, cell=[0, 0])
    cave_warps = {(w['x'], w['z']): w for w in ev.records(ev.raw_member(p, cave_ctx['event_member'], state)) if w['kind'] == 'warp'}
    hut_warp = next(w for w in ev.records(ev.raw_member(p, hut_ctx['event_member'], state)) if w['kind'] == 'warp')
    assert (5, 8) not in cave_warps and cave_warps[(24, 28)]['destination'] == hut
    assert hut_warp['destination'] == cave and hut_warp['destination_warp'] == cave_warps[(24, 28)]['id']
    moved = [c for c in p.diff() if c.get('operation') == 'world.entrance-move']
    assert moved and moved[-1]['moves'][0]['from'] == [5, 8] and moved[-1]['moves'][0]['to'] == [24, 28]
    # The progression view names the exits the move left unconnected (the solid holes behind them are not entrances).
    from sovereign_editor import workspace as ws
    dead = sorted(f['message'].split()[2] for f in ws.progression(p, flood=False)['findings']
                  if f['area'] == cave and 'has no connection' in f['message'])
    assert dead == ['17,28', '5,8']
    reopened = Project(p.root)                                   # replay from disk
    assert (24, 28) in {(w['x'], w['z']) for w in ev.records(ev.raw_member(reopened, cave_ctx['event_member'],
                                                                            reopened.composed())) if w['kind'] == 'warp'}
    p.undo(67)
    assert (5, 8) in {(w['x'], w['z']) for w in ev.records(ev.raw_member(p, cave_ctx['event_member'], p.composed()))
                      if w['kind'] == 'warp'}


def test_connections_tab_moves_an_entrance(tmp_path):
    import os
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtWidgets import QApplication, QWidget
    from sovereign_editor import world_ui
    app = QApplication.instance() or QApplication([])
    p, cave, hut = cave_project(tmp_path)
    p.apply_area_edit(65, operations=[carve(cave), link(cave, 5, 8, hut, 4, 8)], label='carve and link hall')
    inspector = QWidget()
    inspector.project, inspector.header, inspector.cell = p, cave, [0, 0]
    editor = world_ui.WorldEditor(inspector)
    assert editor.move_connection.count() == 2
    editor.move_connection.setCurrentIndex(1); editor.move_source.setText('24,28')
    editor.stage_move()
    assert '[5, 8] → [24, 28] (still paired)' in editor.summary.toPlainText(), editor.summary.toPlainText()
    editor.apply()
    assert p.doc['revision'] == 67
    inspector.deleteLater()
