import copy
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import pytest
from PySide6.QtCore import Qt, QPointF, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit, QCheckBox, QListWidget
from sovereign_editor.core import Project, atomic_json
from sovereign_editor.map_inspector import MapInspectorWindow
from sovereign_editor.gui import STYLE


@pytest.fixture(scope='module')
def app():
    a = QApplication.instance() or QApplication([])
    a.setStyle('Fusion'); a.setStyleSheet(STYLE)
    return a


@pytest.fixture(scope='module')
def workspace(tmp_path_factory):
    p = Project('projects/npc-behavior-1').clone(tmp_path_factory.mktemp('workflow-ui') / 'project')
    return p.root, copy.deepcopy(p.doc)


@pytest.fixture
def w(app, workspace):
    root, doc = workspace; atomic_json(root / 'project.json', doc)
    window = MapInspectorWindow(Project(root), context=(72, [0, 0]))
    window.resize(1180, 800); window.show(); app.processEvents()
    window.inspector_tabs.setCurrentIndex(2); app.processEvents()
    yield window
    window.close(); app.processEvents(); atomic_json(root / 'project.json', doc)


def point(w, x, z):
    return w.grid.mapFromScene(QPointF(x * 32, z * 32))


def drag(w, start, end):
    QTest.mousePress(w.grid.viewport(), Qt.MouseButton.LeftButton, pos=point(w, *start))
    QTest.mouseMove(w.grid.viewport(), point(w, *end))
    QTest.mouseRelease(w.grid.viewport(), Qt.MouseButton.LeftButton, pos=point(w, *end))


def test_direct_paint_sample_rectangle_pending_history_and_apply(w, app):
    original = w.project.path.read_bytes()
    w.wf_tool.setCurrentIndex(3)
    QTest.mouseClick(w.grid.viewport(), Qt.MouseButton.LeftButton, pos=point(w, 2.5, 6.5))
    assert w.wf_material.currentData() == 'lambert9' and w.wf_tool.currentData() == 'brush'
    w.wf_isolate()
    assert w.matrix_input.value() == 288 and w.listing['matrix'] == 288
    w.wf_tool.setCurrentIndex(2); w.wf_material.setCurrentIndex(w.wf_material.findData('lambert12'))
    drag(w, (7.5, 7.5), (8.5, 7.5)); app.processEvents()
    assert len(w.wf.actions) == 2 and len(w.wf.actions[-1]['request']['cells']) == 2
    assert w.project.path.read_bytes() == original and w.wf_apply_button.isEnabled()
    assert not w.inspector_tabs.isTabEnabled(0)
    w.wf_undo(); assert len(w.wf.actions) == 1
    w.wf_redo(); assert len(w.wf.actions) == 2
    w.wf_before.setChecked(True); app.processEvents()
    assert w.view_data['context']['map_member'] == 215
    assert w.matrix_input.value() == 97 and w.listing['matrix'] == 97
    w.wf_before.setChecked(False); app.processEvents()
    assert w.view_data['context']['map_member'] == 676
    assert w.matrix_input.value() == 288 and w.listing['matrix'] == 288
    w.wf_apply(); assert w.project.doc['revision'] == 23
    assert Project(w.project.root).context(header=72, cell=[0, 0])['map_member'] == 676
    w.wf_undo(); assert w.project.context(header=72, cell=[0, 0])['map_member'] == 215
    w.wf_redo(); assert w.project.context(header=72, cell=[0, 0])['map_member'] == 676


def test_canvas_shift_selection_marquee_lock_pan_and_cancel(w, app):
    original = w.project.path.read_bytes()
    QTest.mouseClick(w.grid.viewport(), Qt.MouseButton.LeftButton, pos=point(w, 9.5, 3.5))
    assert w.wf_ids == {'baseline:215:3'}
    QTest.mouseClick(w.grid.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier, pos=point(w, 8, 3.5))
    assert w.wf_ids == {'baseline:215:2', 'baseline:215:3'}
    w.wf_clear_selection(); drag(w, (4.8, 4.8), (9.1, 7.1))
    assert w.wf_ids == {f'baseline:215:{i}' for i in range(4, 9)}
    w.wf_clear_selection(); w.wf_object_lock.setChecked(True)
    drag(w, (4.8, 4.8), (9.1, 7.1)); assert not w.wf_ids
    w.wf_object_lock.setChecked(False)
    QTest.keyPress(w.grid, Qt.Key.Key_Space)
    drag(w, (5.5, 6.5), (6.5, 6.5))
    QTest.keyRelease(w.grid, Qt.Key.Key_Space)
    assert not w.wf.actions and w.project.path.read_bytes() == original
    w.wf_isolate(); w.load_context(70, [0, 0]); assert w.header == 72
    w.wf_event_lock.setChecked(False); w.wf_object_lock.setChecked(True)
    QTest.mouseClick(w.grid.viewport(), Qt.MouseButton.LeftButton, pos=point(w, 5.5, 5.5))
    assert w.selected_event is None and w.wf_active() and len(w.wf.actions) == 1
    w.inspector_tabs.setCurrentIndex(1)
    assert w.wf_active() and w.selected_event is None
    w.wf_cancel(); assert w.project.path.read_bytes() == original and not w.wf.actions


def test_nudge_group_preview_queue_edit_remove_and_compact_footer(w, app):
    w.wf_isolate(); w.wf_ids = {'baseline:676:3'}
    w.grid.setFocus(); QTest.keyClick(w.grid, Qt.Key.Key_Down); app.processEvents()
    assert w.wf.actions[-1]['request']['dz'] == 1
    replacement = copy.deepcopy(w.wf.actions[-1]); replacement['request']['dz'] = 2
    w.wf.replace(1, replacement); w.refresh()
    assert next(p for p in w.view_data['placements'] if p['slot'] == 3)['position']['z'] == 5.5
    w.wf_queue.setCurrentRow(1); w.wf_remove(); assert len(w.wf.actions) == 1
    w.resize(1000, 720); app.processEvents()
    app.processEvents()
    assert w.wf_apply_button.isVisible() and w.wf_apply_button.visibleRegion().boundingRect().height() > 0
    x0, z0, x1, z1 = w.scene_data['content']['tiles']
    for x, z in ((x0, z0), (x1, z1)):
        assert w.grid.viewport().rect().contains(point(w, x, z))
    root = Path('evidence/map-workflow-1/screens')
    assert w.grab().save(str(root / 'compact-workspace.png'))
    w.resize(1280, 850); app.processEvents()
    assert w.grab().save(str(root / 'workspace-independent-preview.png'))


def test_duplicate_click_placement_and_explicit_collision(w, app):
    original = w.project.path.read_bytes()
    w.wf_ids = {'baseline:215:3'}; w.selected_cells = {(9, 3)}; w.wf_move_cells.setChecked(True)
    w.wf_duplicate(); assert w.wf_gesture['kind'] == 'duplicate'
    QTest.mouseMove(w.grid.viewport(), point(w, 9.5, 5.5))
    QTest.mouseClick(w.grid.viewport(), Qt.MouseButton.LeftButton, pos=point(w, 9.5, 5.5))
    app.processEvents()
    assert len(w.wf.actions) == 1 and len(w.wf_ids) == 1 and 'baseline:215:3' not in w.wf_ids
    assert len(w.view_data['placements']) == 11
    assert w.wf.preview.permission_cells(header=72, cell=[0, 0], x=9, z=5)['cells'][0]['collision'] & 128
    assert w.project.path.read_bytes() == original
    w.wf_cancel()


def test_edit_saved_links_retains_full_repair_and_events(w, app):
    w.wf_isolate()
    w.wf_ids = {f'baseline:676:{i}' for i in range(4, 9)}
    cells = [{'x': x, 'z': z} for x in (6, 7) for z in (5, 6)]
    repair = {'cells': [{'x': x, 'z': z} for x in range(5, 9) for z in (5, 6)],
              'material': 'lambert9', 'sample': {'x': 2, 'z': 6}}
    w.wf_stage(w.wf_action('group', dict(action='save', name='Dining set', object_ids=sorted(w.wf_ids),
        cells=cells, repair=repair, events=[{'kind': 'npc', 'event_id': i} for i in (0, 1)])))
    w.wf_groups.setCurrentIndex(1); w.wf_choose_group()
    before = copy.deepcopy(w.wf.preview.linked_groups(**w.wf_ref()))
    observed = {}
    def inspect():
        dialog = app.activeModalWidget()
        observed['name'] = dialog.findChild(QLineEdit, 'group_name').text()
        observed['repair'] = dialog.findChild(QCheckBox, 'group_repair').isChecked()
        observed['label'] = dialog.findChild(QCheckBox, 'group_repair').text()
        observed['links'] = len(dialog.findChild(QListWidget).selectedItems())
        dialog.grab().save('evidence/map-workflow-1/screens/linked-group-dialog.png')
        dialog.accept()
    QTimer.singleShot(0, inspect); w.wf_bind()
    assert observed == {'name': 'Dining set', 'repair': True,
                        'label': 'Repair 8 floor cells when vacated', 'links': 2}
    assert w.wf.preview.linked_groups(**w.wf_ref()) == before
    assert w.wf.plan['actions'][-1]['changes'] == 0


def test_layout_resumes_after_saving_in_existing_event_editor(w, app):
    w.inspector_tabs.setCurrentIndex(1); w.select_event('npc', 0)
    original = w.current_event()['facing']
    w.event_inputs['facing'].setCurrentIndex(w.event_inputs['facing'].findData(0 if original else 1))
    w.preview_event(); w.refresh()
    assert w.event_pending() and w.current_event()['facing'] == original
    w.apply_event()
    assert not w.event_pending() and w.wf.revision == w.project.doc['revision']
    w.inspector_tabs.setCurrentIndex(2)
    w.wf_isolate(); w.wf_apply()
    assert w.project.context(**w.wf_ref())['map_member'] == 676
