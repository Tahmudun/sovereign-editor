"""PROD-CAP-001 UI: on/off states, storage display and the shared capacity line."""
import os
from pathlib import Path

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication

from sovereign_editor import storage
from sovereign_editor.core import Project
from sovereign_editor.gui import STYLE
from sovereign_editor.map_inspector import MapInspectorWindow
from sovereign_editor.story_ui import StoryEditor

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / 'projects/scyther-orchestration-1/baseline.nds'


def test_state_panel_stages_on_off_states_and_shows_capacity(tmp_path):
    if not BASELINE.exists():
        pytest.skip('Pinned baseline is local and untracked')
    app = QApplication.instance() or QApplication([])
    app.setStyle('Fusion'); app.setStyleSheet(STYLE)
    p = Project.create(BASELINE, tmp_path / 'project')
    w = MapInspectorWindow(p, context=(33, [18, 12])); w.show(); app.processEvents()
    d = StoryEditor(w); d.show(); app.processEvents()
    assert 'states 0/158' in d.capacity_label.text()
    d.tabs.setCurrentIndex(2)
    d.state_key.setText('lamp'); d.state_name.setText('Lamp'); d.state_switch.setChecked(True)
    before = p.path.read_bytes()
    d.stage_state(); app.processEvents()
    assert p.path.read_bytes() == before and d.apply_button.isEnabled()
    lamp = d.preview.story_library()['states']['lamp']
    assert lamp['switch'] and lamp['flag'] == storage.STATE_FLAGS[0]
    assert 'on/off' in d.states_list.item(0).text()
    d.state_key.setText('stage'); d.state_name.setText('Stage'); d.state_switch.setChecked(False)
    d.stage_state(); app.processEvents()
    stage = d.preview.story_library()['states']['stage']
    assert stage == {'name': 'Stage', 'variable': 0x4160}
    assert 'states 2/158' in d.capacity_label.text() and 'on/off 1/98' in d.capacity_label.text()
    d.tabs.setCurrentIndex(3); d.step_type.setCurrentText('set'); d.add_step()
    d.step_state.setCurrentIndex(d.step_state.findData('lamp')); app.processEvents()
    assert d.step_value.maximum() == 1
    d.step_state.setCurrentIndex(d.step_state.findData('stage')); app.processEvents()
    assert d.step_value.maximum() == 65535
    d.apply(); app.processEvents()
    assert Project(p.root).story_library()['states']['lamp']['flag'] == storage.STATE_FLAGS[0]
    w.close(); app.processEvents()


def test_world_editor_lays_out_reviews_and_borders_a_4x4_template(tmp_path):
    if not BASELINE.exists():
        pytest.skip('Pinned baseline is local and untracked')
    from sovereign_editor.world_ui import WorldEditor
    app = QApplication.instance() or QApplication([])
    app.setStyle('Fusion'); app.setStyleSheet(STYLE)
    p = Project.create(BASELINE, tmp_path / 'project')
    w = MapInspectorWindow(p, context=(23, [40, 13])); w.show(); app.processEvents()
    d = WorldEditor(w); d.show(); app.processEvents()
    d.identity.setText('canopy_walk'); d.area_name.setText('Canopy Walk'); d.internal_name.setText('CANOPY_WALK')
    d.template.setValue(23); d.close_edges.setChecked(False)
    d.window_matrix.setValue(0); d.window_x.setValue(39); d.window_y.setValue(11)
    d.window_w.setValue(4); d.window_h.setValue(4); d.fill_window(); app.processEvents()
    assert d.layout_table.item(2, 1).text() == '23:40,13' and d.layout_table.item(0, 0).text() == '0:39,11'
    d.review_template(); app.processEvents()
    assert 'internal dependencies missing: 0' in d.summary.toPlainText()
    before = p.path.read_bytes()
    d.stage_area(); app.processEvents()
    assert 'header 540' in d.summary.toPlainText() and '16 cell' in d.summary.toPlainText()
    assert p.path.read_bytes() == before and d.apply_button.isEnabled()
    d.grab().save(str(tmp_path / 'world-4x4.png'))
    assert d.width() <= 1100 and d.layout_table.height() < 400                     # no 16-row form overflow
    d.apply(); app.processEvents(); d.close()
    w.load_context(540, [1, 2]); app.processEvents()
    d = WorldEditor(w); d.tabs.setCurrentIndex(d.tabs.indexOf(d.border_family.parentWidget()))
    d.border_family.setCurrentIndex(d.border_family.findData('path'))
    d.border_tiles.setText('42,70,9,1; 50,71,1,1; 50,72,19,1'); d.border_window.setText('42,69,27,5')
    d.stage_border(); app.processEvents()
    assert 'Border path' in d.summary.toPlainText() and d.apply_button.isEnabled()
    d.border_family.setCurrentIndex(d.border_family.findData('path'))
    d.border_tiles.setText('52,83,1,1; 54,83,1,1'); d.border_window.setText('')
    d.stage_border(); app.processEvents()
    assert 'UNSUPPORTED_BORDER' in d.status.text() and not d.apply_button.isEnabled()
    w.close(); app.processEvents()
