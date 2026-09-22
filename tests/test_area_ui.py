import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
import json
import pytest
from PySide6.QtWidgets import QApplication
from sovereign_editor.core import Project,atomic_json
from sovereign_editor.map_inspector import MapInspectorWindow
from sovereign_editor.area_ui import AreaEditor

@pytest.fixture(scope='module')
def app():return QApplication.instance() or QApplication([])

@pytest.fixture
def window(tmp_path,app):
    p=Project.create('projects/map-events-1/baseline.nds',tmp_path/'project')
    atomic_json(p.path,json.loads(Path('projects/map-events-1/project.json').read_text()))
    w=MapInspectorWindow(Project(p.root),context=(67,[17,12]));w.show();app.processEvents()
    yield w
    w.close();app.processEvents()


def test_surface_dialogue_composer_preview_cancel_apply_undo(window,app,tmp_path):
    d=AreaEditor(window);d.show();app.processEvents();before=window.project.path.read_bytes()
    d.surface_x.setValue(557);d.surface_z.setValue(403);d.material.setCurrentIndex(d.material.findData('road01'))
    d.stage_surface();app.processEvents();assert d.apply_button.isEnabled() and window.project.path.read_bytes()==before
    assert d.before_view.sceneRect().width()>0 and d.after_view.sceneRect().width()>0
    d.tabs.setCurrentIndex(3);d.dialogue_x.setValue(554);d.dialogue_z.setValue(404)
    d.donor.setCurrentIndex(d.donor.findData(2));d.dialogue.setPlainText('The path is open.\nEnjoy your visit!')
    d.stage_dialogue();assert len(d.plan['transactions'])==2 and window.project.path.read_bytes()==before
    d.grab().save(str(tmp_path/'area-composer.png'))
    d.reject();assert window.project.path.read_bytes()==before
    d=AreaEditor(window);d.stage([dict(kind='interaction',context=dict(header=67,cell=[17,12]),request=dict(action='create',kind='npc',donor_id=2,x=554,z=404,dialogue='Hello there.'))])
    d.apply();assert window.project.doc['revision']==19
    assert len(window.project.simple_interactions(header=67,cell=[17,12]))==1
    window.undo();assert not window.project.simple_interactions(header=67,cell=[17,12])
    d.close()


def test_library_load_and_search(window,app):
    d=AreaEditor(window);d.load_sources();assert d.sources.count()>1
    i=next(i for i in range(d.sources.count()) if d.sources.itemData(i)==dict(header=33,cell=[18,12]))
    d.sources.setCurrentIndex(i);d.load_templates();assert d.templates.count()>0
    total=d.templates.count();d.template_search.setText('sign');assert 0<d.templates.count()<=total
    d.template_search.setText('this name is absent');assert d.templates.count()==0
    d.close()
