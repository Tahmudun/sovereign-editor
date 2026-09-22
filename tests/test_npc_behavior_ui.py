import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication,QGraphicsRectItem
from sovereign_editor.core import Project
from sovereign_editor.map_inspector import MapInspectorWindow
from sovereign_editor.area_ui import AreaEditor


def test_behavior_composer_controls_preview_cancel_apply_reopen_undo(tmp_path):
    app=QApplication.instance() or QApplication([])
    p=Project('projects/map-area-authoring-1').clone(tmp_path/'project','NPC UI check')
    w=MapInspectorWindow(p,context=(67,[17,12]));w.show();app.processEvents()
    d=AreaEditor(w);d.show();d.tabs.setCurrentIndex(3)
    d.interaction_action.setCurrentIndex(d.interaction_action.findData('edit'))
    i=next(i for i in range(d.authored.count()) if d.authored.itemData(i)['kind']=='npc')
    d.authored.setCurrentIndex(i);d.load_authored()
    d.appearance.setCurrentIndex(d.appearance.findData(341))
    d.movement.setCurrentIndex(d.movement.findData(5));app.processEvents()
    assert d.range_x.isEnabled() and d.range_x.value()==1
    assert not d.range_z.isEnabled() and d.range_z.value()==0
    before=p.path.read_bytes();d.stage_dialogue();app.processEvents()
    assert p.path.read_bytes()==before
    assert d.authored.currentData()['identity']=='simple:29'
    d.dialogue.setPlainText('I enjoy walking here.');d.stage_dialogue()
    assert d.preview_project.simple_interactions(header=67,cell=[17,12])[-1]['dialogue']=='I enjoy walking here.'
    boundaries=[i for i in d.after_view.scene().items() if isinstance(i,QGraphicsRectItem) and 'movement boundary' in i.toolTip()]
    assert any('±1 X, ±0 Z' in i.toolTip() and i.rect().width()==96 for i in boundaries)
    d.reject();assert p.path.read_bytes()==before
    d=AreaEditor(w);d.stage([dict(kind='interaction',context=dict(header=67,cell=[17,12]),
        request=dict(action='edit',identity='simple:29',sprite=341,movement=5,range_x=1,range_z=0))])
    d.apply();assert p.doc['revision']==22
    d=AreaEditor(w);d.interaction_action.setCurrentIndex(1);d.authored.setCurrentIndex(1);d.load_authored()
    assert d.movement.currentData()==5 and d.appearance.currentData()==341
    assert d.range_x.value()==1
    d.interaction_kind.setCurrentIndex(1);app.processEvents()
    assert not d.movement.isEnabled() and not d.appearance.isEnabled()
    d.reject();w.undo();assert p.doc['map_edits']==Project('projects/map-area-authoring-1').doc['map_edits']
    w.close();app.processEvents()
