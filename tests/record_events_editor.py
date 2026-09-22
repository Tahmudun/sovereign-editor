"""Styled native Qt captures, explicit visual review evidence."""
import json
import os
from pathlib import Path

def test_record(tmp_path):
    os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
    from PySide6.QtWidgets import QApplication
    from sovereign_editor.gui import STYLE
    from sovereign_editor.core import Project,atomic_json
    from sovereign_editor.map_inspector import MapInspectorWindow
    app=QApplication.instance() or QApplication([])
    app.setStyle('Fusion');app.setStyleSheet(STYLE)
    p=Project.create('projects/map-scenery-1/baseline.nds',tmp_path/'project')
    atomic_json(p.path,json.loads(Path('projects/map-scenery-1/project.json').read_text()))
    w=MapInspectorWindow(Project(p.root),context=(67,[17,12]));w.show();app.processEvents()
    out=Path('evidence/map-events-1/screens');out.mkdir(exist_ok=True)
    w.select_event('npc',2);w.focus_tile(562,405,19)
    w.event_inputs['x'].setValue(563);w.event_inputs['z'].setValue(408)
    w.event_inputs['range_x'].setValue(2);w.event_inputs['facing'].setCurrentIndex(1)
    w.preview_event();app.processEvents()
    assert w.grab().save(str(out/'01-npc-preview.png'))
    w.cancel_event();w.select_event('warp',3)
    w.event_inputs['destination'].setValue(72);w.event_reciprocal.setChecked(True)
    w.preview_event();w.focus_tile(558,401,17);app.processEvents()
    assert w.grab().save(str(out/'02-warp-connection.png'))
    w.apply_event();w.open_event_destination();w.resize(1180,760);app.processEvents()
    assert w.header==72
    assert w.grab().save(str(out/'03-destination-compact.png'))
    w.load_context(60,[21,12]);w.select_event('background',2)
    w.focus_tile(684,400,15);app.processEvents()
    assert w.grab().save(str(out/'04-sign-association.png'))
    w.close();app.processEvents()
