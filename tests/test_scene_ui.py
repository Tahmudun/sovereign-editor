"""Native form roundtrips scene fields without rewriting them as legacy events."""
import copy
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtWidgets import QApplication,QWidget
from sovereign_editor.story_ui import StoryEditor
from tests.test_scyther_quest import QuestVM


def editor(cell=(19,12)):
    app=QApplication.instance() or QApplication([])
    vm=QuestVM()
    lib={'characters':{},'trainers':{},'states':vm.defs['state'],'sequences':vm.defs['sequence']}
    for s in lib['sequences'].values():s['context']={'header':s['header'],'cell':[s['x']//32,s['z']//32]}
    class Project:
        doc={'revision':27}
        def story_library(self):return copy.deepcopy(lib)
    parent=QWidget();parent.project=Project();parent.header=33;parent.cell=list(cell)
    d=StoryEditor(parent);captured=[];d.stage=lambda *args:captured.append(copy.deepcopy(args))
    d.tabs.setCurrentIndex(3);d.show();app.processEvents()
    return app,parent,d,captured


def test_scene_form_roundtrip_and_visible_controls(tmp_path):
    app,parent,d,captured=editor()
    d.events.setCurrentIndex(d.events.findData('scyther_corner'));app.processEvents()
    assert d.event_character.currentData()=='#scyther'
    assert d.scene_state.currentData()=='scyther_quest' and d.scene_values.text()=='5, 6, 7'
    original=copy.deepcopy(d.nodes)
    for i,n in enumerate(original):
        d.steps.setCurrentRow(i);app.processEvents();d.save_step()
        expected=copy.deepcopy(n)
        if n['op']=='end':expected['complete']=True
        assert d.nodes[i]==expected
        for _,widget,ops in d.scene_fields:assert widget.isHidden()==(n['op'] not in ops)
    d.stage_event();value=captured[-1][2]
    assert value['stock_sprite']==552 and value['character'] is None
    assert value['presence']=={'state':'scyther_quest','values':[5,6,7]}
    d.events.setCurrentIndex(d.events.findData('corner_find'));d.stage_event();value=captured[-1][2]
    assert value['trigger']=={'state':'scyther_quest','value':5,'width':4,'height':3}
    assert value['facing']==0 and value['character'] is None and value['movement']==0
    assert d.nodes[0]['destination']==[620,407]
    assert d.grab().save(str(tmp_path/'quest-scene-controls.png'))
    d.close();parent.close();app.processEvents()


def test_event_form_shows_only_controls_for_the_selected_trigger_kind():
    app,parent,d,_=editor()
    npc_only=[d.event_character,d.facing,d.movement,d.range_x,d.range_z]
    assert d.entry.currentIndex()==0 and d.trigger_width.isHidden()   # initial NPC form
    d.events.setCurrentIndex(d.events.findData('corner_find'));app.processEvents()
    assert all(w.isHidden() for w in npc_only) and not d.trigger_width.isHidden()
    d.events.setCurrentIndex(d.events.findData('scyther_corner'));app.processEvents()
    assert not any(w.isHidden() for w in npc_only) and d.trigger_width.isHidden()
    d.entry.setCurrentIndex(1);app.processEvents()          # switching the kind applies immediately
    assert d.facing.isHidden() and not d.trigger_height.isHidden()
    d.close();parent.close();app.processEvents()
