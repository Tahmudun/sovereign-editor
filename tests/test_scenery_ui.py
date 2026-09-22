"""Real Qt action staging and Project integration, using isolated scratch projects."""
import json
import os
from pathlib import Path
import pytest

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtWidgets import QApplication
from sovereign_editor.core import Project, atomic_json
from sovereign_editor.map_inspector import MapInspectorWindow


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(tmp_path, app):
    p=Project.create('projects/map-adjacent-1/baseline.nds',tmp_path/'project')
    atomic_json(p.path,json.loads(Path('projects/map-adjacent-1/project.json').read_text()))
    w=MapInspectorWindow(Project(p.root),context=(67,[17,12]))
    w.show();app.processEvents()
    yield w
    w.close();app.processEvents()


def test_duplicate_cancel_apply_reopen_and_delete(window,app):
    w=window
    original=w.project.path.read_bytes()
    w.select_placement(12);w.start_scenery('duplicate')
    assert w.scenery_action and w.apply_button.isEnabled()
    assert w.place_scenery_at(9,14)
    assert w.x_input.anchor_value()==553.5
    w.preview();app.processEvents()
    assert w.scenery_scenes and 'PENDING' in w.detail.toPlainText()
    assert w.project.path.read_bytes()==original
    w.cancel_staged()
    assert not w.scenery_action and not w.scenery_scenes
    assert w.project.path.read_bytes()==original
    w.start_scenery('duplicate');w.x_input.set_anchor(553.5);w.apply_transaction()
    assert len(w.view_data['placements'])==16 and w.selected_slot>=15
    object_id=next(p['object_id'] for p in w.view_data['placements'] if p['slot']==w.selected_slot)
    reopened=MapInspectorWindow(Project(w.project.root))
    assert reopened.header==67 and reopened.cell==[17,12]
    assert next(p['object_id'] for p in reopened.view_data['placements'] if p['slot']==reopened.selected_slot)==object_id
    reopened.close()
    # The normal movement controls also operate on a newly inserted record.
    w.x_input.setValue(554.5);w.apply_transaction()
    assert next(o for o in w.view_data['placements'] if o['object_id']==object_id)['position']['x']==554.5
    assert '0 ROM byte' not in w.message.text()
    w.start_scenery('delete');w.apply_transaction()
    assert len(w.view_data['placements'])==15
    w.undo();assert len(w.view_data['placements'])==16


def test_palette_and_transfer_share_project_plan(window,app):
    w=window;w.open_palette();app.processEvents()
    assert w.palette_dialog.isVisible() and w.palette_list.count()>0
    w.palette_dialog.close()
    w.start_scenery('add',slot=12)
    w.place_scenery_at(9,14);w.apply_transaction()
    slot=w.selected_slot
    w.destination_box.setCurrentIndex(next(i for i in range(w.destination_box.count()) if w.destination_box.itemData(i)=={'header':33,'cell':[18,12]}))
    w.start_scenery('transfer');assert w.scenery_action
    w.x_input.set_anchor(578.5);w.z_input.set_anchor(398.5)
    old=w.project.path.read_bytes()
    w.load_context(60,[21,12]);assert w.header==67 # staged action is retained
    w.preview();assert w.project.path.read_bytes()==old
    w.apply_transaction()
    assert w.header==33 and w.cell==[18,12]
    assert len(w.view_data['placements'])==2
    assert w.selected_slot is not None
    assert not w.scenery_action


def test_double_click_selects_blocker_without_moving_staged_anchor(window,app):
    from PySide6.QtCore import Qt, QPointF
    from PySide6.QtTest import QTest
    w=window
    w.select_placement(12);w.start_scenery('duplicate')
    w.x_input.set_anchor(553.5);w.z_input.set_anchor(398.5)
    w.focus_tile(552,398,12);app.processEvents()
    point=w.grid.mapFromScene(QPointF((551.5-544)*32,(398.5-384)*32))
    QTest.mouseClick(w.grid.viewport(),Qt.MouseButton.LeftButton,pos=point)
    QTest.mouseDClick(w.grid.viewport(),Qt.MouseButton.LeftButton,pos=point)
    app.processEvents()
    assert (551,398) in w.selected_cells
    assert (w.x_input.anchor_value(),w.z_input.anchor_value())==(553.5,398.5)
    w.move_collision.setChecked(True)
    plan=w.project.plan_scenery_edit(**w.scenery_request())
    assert plan['transaction']['to']['x']==553.5
    assert [(c['x'],c['z']) for c in plan['transaction']['permissions']]==[(553,398)]
