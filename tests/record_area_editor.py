"""Explicit area-composer screenshots for human/model visual review."""
import json,os
from pathlib import Path

def test_record(tmp_path):
    os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
    from PySide6.QtWidgets import QApplication
    from sovereign_editor.gui import STYLE
    from sovereign_editor.core import Project
    from sovereign_editor.map_inspector import MapInspectorWindow
    from sovereign_editor.area_ui import AreaEditor
    app=QApplication.instance() or QApplication([]);app.setStyle('Fusion');app.setStyleSheet(STYLE)
    p=Project('projects/map-area-authoring-1');assert p.doc['revision']==20
    w=MapInspectorWindow(p,context=(67,[17,12]));w.show();app.processEvents()
    d=AreaEditor(w);d.show();app.processEvents()
    request=json.loads(Path('evidence/map-area-authoring-1/candidate-request.json').read_text())
    d.stage(request['operations']);app.processEvents()
    root=Path('evidence/map-area-authoring-1/screens');root.mkdir(exist_ok=True)
    assert d.grab().save(str(root/'01-town-area-preview.png'))
    d.tabs.setCurrentIndex(1);app.processEvents();assert d.grab().save(str(root/'02-group-controls.png'))
    d.tabs.setCurrentIndex(3);d.interaction_action.setCurrentIndex(1);d.authored.setCurrentIndex(1);d.load_authored();app.processEvents()
    assert d.grab().save(str(root/'03-dialogue-controls.png'))
    d.tabs.setCurrentIndex(2);d.load_sources()
    index=next(i for i in range(d.sources.count()) if d.sources.itemData(i)==dict(header=33,cell=[18,12]))
    d.sources.setCurrentIndex(index);d.load_templates();app.processEvents()
    assert d.grab().save(str(root/'04-library-controls.png'))
    d.resize(1000,720);app.processEvents();assert d.grab().save(str(root/'05-compact-area.png'))
    d.reject();w.close();app.processEvents()
    assert p.doc['revision']==20
