"""Real Qt event staging, map picking and destination navigation."""
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
    p=Project.create('projects/map-scenery-1/baseline.nds',tmp_path/'project')
    atomic_json(p.path,json.loads(Path('projects/map-scenery-1/project.json').read_text()))
    w=MapInspectorWindow(Project(p.root),context=(67,[17,12]))
    w.show();app.processEvents()
    yield w
    w.close();app.processEvents()


def test_npc_overlay_preview_cancel_apply_undo(window,app):
    from PySide6.QtCore import Qt, QPointF
    from PySide6.QtTest import QTest
    w=window
    before=w.project.path.read_bytes()
    w.focus_tile(561,407,14);app.processEvents()
    point=w.grid.mapFromScene(QPointF((561.5-544)*32,(407.5-384)*32))
    QTest.mouseClick(w.grid.viewport(),Qt.MouseButton.LeftButton,pos=point)
    assert w.selected_event==('npc',2) and w.inspector_tabs.currentIndex()==1
    w.event_inputs['x'].setValue(562);w.event_inputs['z'].setValue(408)
    w.event_inputs['range_x'].setValue(0)
    assert w.event_pending() and w.event_apply_button.isEnabled()
    w.preview_event()
    assert 'PREVIEW' in w.event_detail.toPlainText() and w.project.path.read_bytes()==before
    w.refresh();assert w.event_pending() and w.event_inputs['x'].value()==562
    w.select_event('npc',2);assert w.event_pending()
    w.set_tool('block');assert w.tool=='select'
    w.load_context(60,[21,12]);assert w.header==67 and w.event_pending()
    w.select_event('warp',3);assert w.selected_event==('npc',2)
    w.inspector_tabs.setCurrentIndex(0);assert w.inspector_tabs.currentIndex()==1
    w.cancel_event();assert not w.event_pending() and w.project.path.read_bytes()==before
    w.event_inputs['x'].setValue(562);w.event_inputs['z'].setValue(408);w.event_inputs['range_x'].setValue(0)
    w.apply_event();assert w.project.doc['revision']==14 and not w.event_pending()
    assert w.current_event()['x']==562 and 'SAVED' in w.event_detail.toPlainText()
    reopened=Project(w.project.root)
    assert next(e for e in reopened.map_events(header=67,cell=[17,12])['events'] if (e['kind'],e['id'])==('npc',2))['x']==562
    opened=MapInspectorWindow(reopened)
    assert opened.header==67 and opened.selected_event==('npc',2)
    assert opened.inspector_tabs.currentIndex()==1
    opened.close()
    w.undo();assert w.project.doc['revision']==15
    w.select_event('npc',2);assert w.current_event()['x']==561


def test_warp_preview_both_ends_apply_navigation(window,app):
    w=window
    w.select_event('warp',3)
    assert w.event_target['header']==71
    w.event_inputs['destination'].setValue(72)
    w.event_reciprocal.setChecked(True)
    w.preview_event()
    assert w.event_target['header']==72 and w.event_target['returns_to_source']
    assert 'header 72' in w.event_detail.toPlainText()
    w.open_event_destination();assert w.header==67
    w.apply_event();assert w.project.doc['revision']==14
    w.open_event_destination()
    assert w.header==72 and w.selected_event==('warp',0)
    assert w.current_event()['destination_warp']==3
    w.open_event_destination()
    assert w.header==67 and w.selected_event==('warp',3)


def test_general_background_and_associated_model(window,app):
    w=window;w.select_event('background',1)
    assert w.event_inputs['x'].isVisible() and not w.event_inputs['facing'].isVisible()
    w.event_inputs['x'].setValue(553);w.preview_event()
    assert '554 → 553' in w.event_detail.toPlainText()
    w.apply_event();assert w.current_event()['x']==553
    w.load_context(60,[21,12]);w.select_event('background',2)
    assert w.event_model_button.isVisible()
    w.select_event_model();assert w.selected_slot==13 and w.inspector_tabs.currentIndex()==0


def test_main_window_opens_mixed_event_project(window,app):
    from sovereign_editor.gui import EditorWindow
    w=window
    w.select_event('npc',2);w.event_inputs['facing'].setCurrentIndex(1);w.apply_event()
    old=EditorWindow(w.project.root)
    assert old.project is not None
    assert 'edit npc 64:2' in old.change_text.toPlainText()
    assert next(n for n in old.scene_data['npcs'] if n['id']==2)['changed']
    old.close()


def test_overlapping_sign_selection_follows_active_editor(window,app):
    from PySide6.QtCore import Qt,QPointF
    from PySide6.QtTest import QTest
    w=window;w.load_context(60,[21,12]);w.focus_tile(684,400,10);app.processEvents()
    point=w.grid.mapFromScene(QPointF((684.5-672)*32,(400.5-384)*32))
    w.inspector_tabs.setCurrentIndex(0)
    QTest.mouseClick(w.grid.viewport(),Qt.MouseButton.LeftButton,pos=point)
    assert w.selected_slot==13 and w.selected_event is None
    w.inspector_tabs.setCurrentIndex(1)
    QTest.mouseClick(w.grid.viewport(),Qt.MouseButton.LeftButton,pos=point)
    assert w.selected_event==('background',2) and w.selected_slot is None
    w.set_tool('block')
    assert w.inspector_tabs.currentIndex()==0 and w.selected_event is None
