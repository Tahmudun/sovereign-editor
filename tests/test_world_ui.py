"""World editor dialog: the same Project operations as `area-edit`, previewed then applied."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from sovereign_editor.core import Project
from sovereign_editor.map_inspector import MapInspectorWindow
from sovereign_editor.world_ui import WorldEditor

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT/'projects/scyther-orchestration-1/baseline.nds'


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(tmp_path, app):
    if not BASELINE.exists():
        pytest.skip('Pinned baseline is local and untracked')
    p = Project.create(BASELINE, tmp_path/'project')
    w = MapInspectorWindow(Project(p.root), context=(33, [19, 12]))
    w.show(); app.processEvents()
    yield w
    w.close(); app.processEvents()


def test_world_editor_previews_applies_connects_and_edits_terrain(window, app, tmp_path):
    d = WorldEditor(window); d.show(); app.processEvents()
    before = window.project.path.read_bytes()
    d.identity.setText('survey_field'); d.area_name.setText('Survey Field'); d.internal_name.setText('SURVEY_FIELD')
    d.template.setValue(33)
    d.set_layout_cell(0, 0, '33:18,12'); d.set_layout_cell(1, 0, '33:19,12')   # 4x4 layout table (PROD-VIS-001)
    d.stage_area(); app.processEvents()
    assert 'header 540' in d.summary.toPlainText() and d.apply_button.isEnabled()
    assert window.project.path.read_bytes() == before
    d.grab().save(str(tmp_path/'world-areas.png'))
    d.apply(); app.processEvents()
    assert window.project.header_count() == 541 and d.areas.count() == 1
    d.identity.setText('other'); d.internal_name.setText('bad name'); d.stage_area()
    assert not d.apply_button.isEnabled() and 'INVALID_INPUT' in d.status.text()
    d.areas.setCurrentRow(0); d.open_area(); app.processEvents()
    assert (window.header, window.cell) == (540, [0, 0])
    d.close()
    window.load_context(540, [1, 0]); app.processEvents()
    d = WorldEditor(window); d.tabs.setCurrentIndex(2)
    d.t_x.setValue(52); d.t_z.setValue(20); d.t_w.setValue(3); d.t_h.setValue(2)
    d.material.setCurrentIndex(d.material.findData('egrass')); d.ground.setCurrentIndex(d.ground.findData('grass'))
    d.stage_terrain(); app.processEvents()
    assert 'Visible ground: 6 tile(s)' in d.summary.toPlainText() and d.apply_button.isEnabled()
    d.apply(); app.processEvents()
    cells = window.project.permission_cells(header=540, cell=[1, 0], x=52, z=20, width=3, height=2)['cells']
    assert all(c['type'] == 2 for c in cells)
    d.tabs.setCurrentIndex(1)
    d.source_x.setValue(40); d.source_z.setValue(20); d.destination.setValue(33); d.stage_connection()
    assert 'UNSUPPORTED_ENTRANCE' in d.status.text()
    assert d.links.count() == 0
    d.grab().save(str(tmp_path/'world-terrain.png'))
    d.close()


def test_world_editor_stamps_a_footprint_and_links_wide_entrances(window, app):
    d = WorldEditor(window)
    d.identity.setText('survey_field'); d.area_name.setText('Survey Field'); d.internal_name.setText('SURVEY_FIELD')
    d.template.setValue(33)
    d.set_layout_cell(0, 0, '33:18,12'); d.set_layout_cell(1, 0, '33:19,12')   # 4x4 layout table (PROD-VIS-001)
    d.stage_area(); d.apply(); d.close()
    window.load_context(540, [0, 0]); app.processEvents()
    d = WorldEditor(window); d.tabs.setCurrentIndex(2)
    d.t_x.setValue(2); d.t_z.setValue(12); d.t_w.setValue(6); d.t_h.setValue(6)
    d.stamp.setChecked(True); d.stamp_header.setValue(33); d.stamp_cell.setText('19,12'); d.stamp_x.setValue(624); d.stamp_z.setValue(384)
    d.stage_terrain(); assert d.apply_button.isEnabled(); d.apply(); app.processEvents()
    cells = window.project.permission_cells(header=540, cell=[0, 0], x=4, z=17, width=2, height=1)['cells']
    assert [c['value'] for c in cells] == ['6e06', '6e06']
    d.tabs.setCurrentIndex(1)
    d.source_x.setValue(4); d.source_z.setValue(17); d.extra.setText('5,17')
    d.destination.setValue(33); d.arrival_x.setValue(626); d.arrival_z.setValue(389); d.arrival_extra.setText('627,389')
    d.stage_connection()
    assert 'EVENT_CONFLICT' in d.status.text()      # the stock Route 46 gate tiles already hold warps
    d.close()
