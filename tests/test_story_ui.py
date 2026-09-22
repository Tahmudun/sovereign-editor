import json
import os
from pathlib import Path
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtWidgets import QApplication
from sovereign_editor.core import Project
from sovereign_editor.map_inspector import MapInspectorWindow
from sovereign_editor.story_ui import StoryEditor
from sovereign_editor.gui import STYLE


def test_story_forms_preview_cancel_and_compiled_frames(tmp_path):
    app=QApplication.instance() or QApplication([]);app.setStyle('Fusion');app.setStyleSheet(STYLE)
    from tools.tiana_encounter import operations
    source=Project('projects/map-workflow-1');p=source.clone(tmp_path/'ui-project')
    p.apply_area_edit(23,operations=operations(json.loads(Path('tests/fixtures/tiana.character.json').read_text())))
    w=MapInspectorWindow(p,context=(72,[0,0]));w.show();app.processEvents();d=StoryEditor(w);d.show();app.processEvents()
    assert d.characters.count()==1 and len(d.frames)==16
    for index,frames in ((1,1),(2,6),(0,16)):
        d.animation.setCurrentIndex(index);app.processEvents();assert len(d.frames)==frames
    out=Path('evidence/tiana-events-1/screens');out.mkdir(exist_ok=True)
    assert d.grab().save(str(out/'characters.png'))
    d.tabs.setCurrentIndex(1);d.trainers.setCurrentIndex(1);app.processEvents();assert d.trainer_name.text()=='Tiana'
    assert d.trainer_character.currentData()=='custom:tiana'
    before=p.path.read_bytes();d.stage_trainer();assert p.path.read_bytes()==before and not d.apply_button.isEnabled()
    assert d.grab().save(str(out/'trainers.png'))
    d.tabs.setCurrentIndex(3);d.events.setCurrentIndex(d.events.findData('tiana_practice'));app.processEvents()
    assert d.event_key.text()=='tiana_practice' and len(d.nodes)==12 and d.range_x.value()==1
    assert d.grab().save(str(out/'events.png'))
    d.resize(1000,720);app.processEvents();assert d.grab().save(str(out/'compact-events.png'))
    before=p.path.read_bytes();d.tabs.setCurrentIndex(2);d.state_key.setText('test_state');d.state_name.setText('Test state')
    d.stage('state','test_state',{'name':'Test state'});assert d.apply_button.isEnabled() and p.path.read_bytes()==before
    d.clear();assert p.path.read_bytes()==before and 'test_state' not in d.preview.story_library()['states']
    d.stage('state','test_state',{'name':'Test state'});d.apply();assert 'test_state' in Project(p.root).story_library()['states']
    p.undo(p.doc['revision']);assert 'test_state' not in p.story_library()['states']
    w.close();app.processEvents()
