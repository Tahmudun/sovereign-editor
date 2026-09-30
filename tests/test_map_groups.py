"""Reusable map groups (GROUP-01/02): capture, copy, move, remove, entrances and undo. Software only."""
from pathlib import Path

import pytest

from sovereign_editor import map_groups as mg, props, simple_interactions as si, world
from sovereign_editor.core import Project
from sovereign_editor.formats import EditorError

ROOT = Path(__file__).resolve().parents[1]
PARENT = ROOT / 'projects/editor-completion-v1'
pytestmark = pytest.mark.skipif(not PARENT.is_dir(), reason='r82 parent absent')
CG = {'header': 67, 'cell': [17, 12]}
CW = {'header': 542, 'cell': [2, 2]}
ROOM = {'header': 667, 'cell': [0, 0]}
HOUSE = [{'x': x, 'z': z} for x in range(76, 80) for z in range(80, 85)]


def edit(p, ctx, *requests, extra=()):
    return p.apply_area_edit(p.doc['revision'], operations=list(extra) + [
        {'kind': 'map_group', 'context': ctx, 'request': r} for r in requests])


def test_grove_with_sign_copy_move_remove_and_single_undo(tmp_path):
    p = Project(PARENT).clone(tmp_path / 'p')
    edit(p, CG, {'action': 'capture', 'name': 'pair_grove', 'anchor': {'x': 559, 'z': 393},
                 'props': ['tree_north', 'tree_sw'], 'interactions': ['simple:28']})
    rep = p.map_group_view()['templates']['pair_grove']['report']
    assert rep['shared_assets'] == ['blossom'] and rep['copied']['prop_instances'] == 2 and rep['copied']['sign_npc_text'] == 1
    with pytest.raises(EditorError, match='already blocked'):
        edit(p, CG, {'action': 'place', 'name': 'pair_grove', 'instance': 'grove_b', 'x': 566, 'z': 388})
    before = p.doc['revision']
    edit(p, CG, {'action': 'place', 'name': 'pair_grove', 'instance': 'grove_b', 'x': 563, 'z': 393})
    assert p.doc['revision'] == before + 1                          # one area edit: one undo step
    inst = p.map_group_view()['instances']['grove_b']
    assert inst['props'] == ['grove_b_0', 'grove_b_1'] and len(inst['interactions']) == 1
    placed = props.instances(p.composed())
    assert (placed['grove_b_0']['x'], placed['grove_b_0']['z']) == (563.5, 393.5)
    sign = si.specs(p.composed())[inst['interactions'][0]]
    assert (sign['x'], sign['z'], sign['dialogue']) == (563, 402, si.specs(p.composed())['simple:28']['dialogue'])
    edit(p, CG, {'action': 'move', 'instance': 'grove_b', 'x': 564, 'z': 393})
    assert props.instances(p.composed())['grove_b_1']['x'] == placed['grove_b_1']['x'] + 1
    assert si.specs(p.composed())[inst['interactions'][0]]['x'] == 564
    with pytest.raises(EditorError, match='already there'):
        edit(p, CG, {'action': 'move', 'instance': 'grove_b', 'x': 564, 'z': 393})
    edit(p, CG, {'action': 'remove', 'instance': 'grove_b'})
    assert 'grove_b' not in mg.placed(p.composed()) and 'grove_b_0' not in props.instances(p.composed())
    assert inst['interactions'][0] not in si.specs(p.composed())
    reopened = Project(p.root)
    assert mg.view(reopened.composed()) == mg.view(p.composed())
    p.undo(p.doc['revision'])
    assert 'grove_b' in mg.placed(p.composed()) and 'grove_b_1' in props.instances(p.composed())
    # Forgetting the template keeps copies already placed.
    edit(p, CG, {'action': 'forget', 'name': 'pair_grove'})
    assert 'pair_grove' not in mg.templates(p.composed()) and 'grove_b' in mg.placed(p.composed())


def test_building_with_its_own_entrance_both_ends_coherent(tmp_path):
    p = Project(PARENT).clone(tmp_path / 'p')
    edit(p, CW, {'action': 'capture', 'name': 'house', 'anchor': {'x': 77, 'z': 84}, 'objects': [510, 511],
                 'cells': HOUSE, 'entrances': [{'x': 77, 'z': 84}]})
    rep = p.map_group_view()['templates']['house']['report']
    assert rep['copied']['objects'] == 2 and rep['copied']['tiles'] == 20 and 'new two-way' in rep['entrance']
    room = p.context(**ROOM)
    off = world.cell_offset(room, 5, 8)
    pair = p.member_raw(room['map_member'])[off:off + 2]
    mat = {'kind': 'map', 'context': ROOM, 'request': {'permissions': [{'x': 5, 'z': 8, 'before': pair.hex(),
                                                                        'after': '6500'}]}}
    # A copy never shares the original interior's return warp: it needs its own arrival.
    with pytest.raises(EditorError, match='not a door, exit mat'):
        edit(p, CW, {'action': 'place', 'name': 'house', 'instance': 'house_b', 'x': 86, 'z': 82,
                     'entrance': {'destination': {'header': 667}, 'arrival': {'x': 5, 'z': 8}}})
    # Copies may not bury story actors (final validation).
    with pytest.raises(EditorError, match='blocked terrain'):
        edit(p, CW, {'action': 'place', 'name': 'house', 'instance': 'house_b', 'x': 81, 'z': 84,
                     'entrance': {'destination': {'header': 667}, 'arrival': {'x': 5, 'z': 8}}}, extra=[mat])
    ctx = p.context(**CW)
    original = {(x, z): mg._pair(p, ctx, p.composed(), x, z) for x in range(84, 90) for z in range(78, 83)}
    edit(p, CW, {'action': 'place', 'name': 'house', 'instance': 'house_b', 'x': 86, 'z': 82,
                 'entrance': {'destination': {'header': 667}, 'arrival': {'x': 5, 'z': 8}}}, extra=[mat])
    inst = mg.placed(p.composed())['house_b']
    assert len(inst['objects']) == 2 and len(inst['cells']) == 20 and inst['connection'] is not None

    def warps():
        conn = next(c for c in p.composed()['world']['connections'] if c['index'] == inst['connection'])
        return sorted((w['header'], w['x'], w['z'], w['destination']) for w in conn['warps'])
    assert warps() == [(542, 86, 82, 667), (667, 5, 8, 542)]
    state = p.composed()
    tiles = {(x, z): mg._pair(p, ctx, state, x, z) for x in range(85, 89) for z in range(78, 83)}
    assert tiles[(86, 82)] == '6980' and all(v.endswith('80') for v in tiles.values())
    edit(p, CW, {'action': 'move', 'instance': 'house_b', 'x': 85, 'z': 82})
    assert warps() == [(542, 85, 82, 667), (667, 5, 8, 542)]
    state = p.composed()
    # The vacated column gets back what was under the house; the footprint moved with its door.
    assert all(mg._pair(p, ctx, state, 88, z) == original[(88, z)] for z in range(78, 83))
    assert mg._pair(p, ctx, state, 85, 82) == '6980' and mg._pair(p, ctx, state, 84, 78).endswith('80')
    with pytest.raises(EditorError, match='permanent'):
        edit(p, CW, {'action': 'remove', 'instance': 'house_b'})


def test_world_editor_groups_tab(tmp_path):
    import os
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    from sovereign_editor.map_inspector import MapInspectorWindow
    from sovereign_editor.world_ui import WorldEditor
    p = Project(PARENT).clone(tmp_path / 'p')
    app = QApplication.instance() or QApplication([])
    w = MapInspectorWindow(p, context=(67, [17, 12])); w.show(); app.processEvents()
    d = WorldEditor(w); d.tabs.setCurrentIndex(d.tabs.indexOf(d.group_list.parentWidget())); d.show()
    app.processEvents()
    keys = [d.group_props.item(i).data(Qt.ItemDataRole.UserRole) for i in range(d.group_props.count())]
    assert {'tree_north', 'tree_sw', 'lantern_north'} <= set(keys)
    for i in range(d.group_props.count()):
        d.group_props.item(i).setSelected(d.group_props.item(i).data(Qt.ItemDataRole.UserRole) in ('tree_north', 'tree_sw'))
    d.group_name.setText('pair_grove'); d.group_anchor[0].setValue(559); d.group_anchor[1].setValue(393)
    before = p.path.read_bytes()
    d.stage_group_capture(); app.processEvents()
    assert d.apply_button.isEnabled() and 'shared' in d.summary.toPlainText() and p.path.read_bytes() == before
    d.apply(); app.processEvents()
    assert 'template pair_grove' in d.group_list.toPlainText()
    d.group_template.setCurrentIndex(d.group_template.findData('pair_grove'))
    d.group_instance.setText('grove_b'); d.group_target[0].setValue(563); d.group_target[1].setValue(393)
    d.stage_group_place(); app.processEvents()
    assert d.apply_button.isEnabled(), d.status.text()
    d.apply(); app.processEvents()
    assert 'copy grove_b of pair_grove' in d.group_list.toPlainText()
    d.grab().save(str(ROOT / 'work/original-content-v1/impl/groups-tab.png'))
    d.close(); w.close(); app.processEvents()


GARDEN = ROOT / 'work/original-content-v1/art/garden-kit/src'


@pytest.mark.skipif(not GARDEN.is_dir(), reason='garden kit sources absent')
def test_framed_bed_and_fence_run_groups(tmp_path):
    """GROUP-01: a framed flower bed and an L-shaped picket fence run (garden-kit props) are
    captured, copied, moved and removed as groups; copies block exactly their prop tiles. Every
    segment is one map object (32 per map cell), which bounds how long a run can be."""
    p = Project(PARENT).clone(tmp_path / 'p')
    place = lambda asset, key, x, z, collision: {'kind': 'prop', 'context': CG, 'request': {
        'action': 'place', 'asset': asset, 'instance': key, 'x': x + 0.5, 'z': z + 0.5, 'collision': collision,
        'expected_revision': 1}}
    bed = [[dx, dz] for dz in range(4) for dx in range(3)]
    fence = [place('fencex', f'fx{i}', 551 + i, 398, [[0, 0]]) for i in range(2)] + [place('fencez', 'fz0', 551, 397, [[0, 0]])]
    p.apply_area_edit(p.doc['revision'], operations=[
        {'kind': 'prop', 'context': CG, 'request': {'source': str(GARDEN / n)}} for n in ('bed', 'fencex', 'fencez')]
        + [place('bed', 'bed1', 551, 392, bed)] + fence, label='Garden')
    edit(p, CG, {'action': 'capture', 'name': 'framed_bed', 'anchor': {'x': 551, 'z': 392}, 'props': ['bed1']},
         {'action': 'capture', 'name': 'fence_run', 'anchor': {'x': 551, 'z': 398},
          'props': ['fx0', 'fx1', 'fz0']})
    view = p.map_group_view()['templates']
    assert view['framed_bed']['report']['shared_assets'] == ['bed']
    assert view['fence_run']['report']['shared_assets'] == ['fencex', 'fencez']
    assert view['fence_run']['report']['copied']['prop_instances'] == 3
    ctx = p.context(**CG)
    blocked = lambda x, z: p.composed()['permissions'].get(
        (ctx['map_member'], world.cell_offset(ctx, x, z)), p.member_raw(ctx['map_member'])[
            world.cell_offset(ctx, x, z):world.cell_offset(ctx, x, z) + 2])[1] & 0x80
    edit(p, CG, {'action': 'place', 'name': 'framed_bed', 'instance': 'bed_b', 'x': 561, 'z': 392},
         {'action': 'place', 'name': 'fence_run', 'instance': 'run_b', 'x': 551, 'z': 404})
    assert all(blocked(561 + dx, 392 + dz) for dx, dz in bed) and not blocked(564, 392)
    placed = props.instances(p.composed())
    run = p.map_group_view()['instances']['run_b']['props']
    assert sorted((placed[k]['x'], placed[k]['z']) for k in run) == [
        (551.5, 403.5), (551.5, 404.5), (552.5, 404.5)]
    edit(p, CG, {'action': 'move', 'instance': 'run_b', 'x': 551, 'z': 405})
    assert blocked(551, 405) and not blocked(552, 404)                 # vacated tiles are walkable again
    with pytest.raises(EditorError, match='already blocked'):
        edit(p, CG, {'action': 'place', 'name': 'framed_bed', 'instance': 'bed_c', 'x': 546, 'z': 395})  # a house
    edit(p, CG, {'action': 'remove', 'instance': 'bed_b'})
    assert not blocked(561, 392) and 'bed_b_0' not in props.instances(p.composed())
    p.undo(p.doc['revision'])
    assert blocked(561, 392) and mg.view(Project(p.root).composed()) == mg.view(p.composed())
