"""Presentation contracts exercise actual emitted bytecode and map qualification."""
import copy

import pytest

from sovereign_editor import event_sequences as es, scene_authoring as sa
from sovereign_editor.formats import EditorError
from tests.test_scene_safety import setup, finish
from tests.test_scyther_quest import QuestVM, reach_corner


@pytest.mark.parametrize('position,axis,direction', [
    ((5,2),'x',0), ((5,8),'x',1), ((2,5),'z',2), ((8,5),'z',3),
    ((2,2),'x',2), ((2,2),'z',0), ((8,8),'x',3), ((8,8),'z',1)])
@pytest.mark.parametrize('actor,target', [('player','a'),('a','player')])
def test_live_gaze_reads_both_positions(position,axis,direction,actor,target):
    nodes=[dict(id='look',op='look',actor=actor,target=target,axis=axis,next='end'),dict(id='end',op='end')]
    code=es.compile_sequence({'kind':'trigger','nodes':nodes},{},{},1,{'actors':{'a':{'npc_id':1}}})
    vm=QuestVM();who=255 if actor=='player' else 1;subject=1 if target=='a' else 255
    vm.positions={who:(5,5),subject:position};vm.facings={who:direction^1,subject:0}
    vm.execute(code)
    assert vm.facings[who]==direction and vm.positions[who]==(5,5) and vm.positions[subject]==position


def test_pose_refuses_a_missing_turn_and_traces_stage_replacement(monkeypatch):
    actors={'a':dict(kind='npc',x=2,z=2,npc_id=1,event_member=1,facing=1,
                    presence={'state':'stage','values':[0]}),
            'b':dict(kind='npc',x=2,z=2,npc_id=2,event_member=1,facing=1,
                    presence={'state':'stage','values':[1]})}
    nodes=[dict(id='face',op='face',actor='a',direction=2,next='before'),
           dict(id='before',op='pose',actors={'a':{'tile':[2,2],'facing':2}},next='advance'),
           dict(id='advance',op='set',state='stage',value=1,next='sync'),
           dict(id='sync',op='sync',next='after'),
           dict(id='after',op='pose',actors={'a':{'visible':False},'b':{'tile':[2,2],'facing':2}},next='end'),
           dict(id='end',op='end')]
    args=setup(monkeypatch,nodes,actors=actors)
    with pytest.raises(EditorError,match='after: b facing'):sa.validate_routes(*args)
    actors['b']['facing']=2;trace=[];sa.validate_routes(*args,trace=trace)
    assert next(b for b in trace if b['step']=='after')['actors']['b']['facing']==[2]
    nodes[0]['direction']=1
    with pytest.raises(EditorError,match='before: a facing'):sa.validate_routes(*args)


def test_npc_gather_routes_cover_each_free_talk_side(monkeypatch):
    nodes=[dict(id='gather',op='gather',destination=[0,2],next='end'),dict(id='end',op='end')]
    p,state,s=setup(monkeypatch,nodes)
    s.update(state['story']['sequence']['a']);s.update(kind='npc',x=2,z=2)
    s.pop('trigger');s['nodes']=nodes
    routes=sa.gather_routes(p,state,s)['gather']
    assert set(routes)=={(1,2),(3,2),(2,1),(2,3)}
    assert all(path[-1]==(0,2) and (2,2) not in path for path in routes.values())
    sa.validate_routes(p,state,s,2)


def test_beach_dialogue_pose_and_stage_handoff_are_executed():
    vm=QuestVM();vm.set('scyther_quest',1);vm.event('beach_find')
    page=next(p for p in vm.presentations if p['kind']=='message')
    tiana=vm.defs['sequence']['tiana_search']['npc_id']
    assert page['positions'][255]==(544,401) and page['facings'][255]==1
    assert page['positions'][tiana]==(545,403) and page['facings'][tiana]==0
    replacement=vm.defs['sequence']['tiana_beach']['npc_id']
    assert vm.facings[replacement]==2 and vm.facings[255]==2


def test_tiana_is_not_removed_until_her_offscreen_walk_finishes():
    vm=QuestVM();reach_corner(vm);vm.event('scyther_corner',[0,0])
    tiana=vm.defs['sequence']['tiana_corner']['npc_id']
    hides=[p for p in vm.presentations if p['event']=='scyther_corner' and p['kind']=='hide' and p['actor']==tiana]
    assert len(hides)==1 and hides[0]['positions'][tiana]==(608,408)
    assert hides[0]['positions'][255]==(620,407) and hides[0]['facings'][tiana]==2


def test_invalid_gaze_and_pose_payloads_are_rejected():
    for node in [dict(id='n',op='look',actor='player',target='player',axis='x',next='end'),
                 dict(id='n',op='pose',actors={'player':{'facing':True}},next='end')]:
        with pytest.raises(EditorError):es.validate([node,dict(id='end',op='end')],{},{})


@pytest.mark.parametrize('failure',[False,True])
def test_collection_is_after_successful_grant_and_before_either_actor_departure(failure):
    vm=QuestVM();vm.collection_failure=failure;reach_corner(vm);vm.event('scyther_corner',[0,0])
    timeline=[p for p in vm.presentations if p['event']=='scyther_corner']
    collect=next(i for i,p in enumerate(timeline) if p['kind']=='collect')
    assert all(p['kind']!='hide' for p in timeline[:collect])
    assert vm.log.index(137)<vm.log.index(853) and vm.gifts==1 and vm.get('scyther_quest')==8
    vm.load(33);assert not vm.alive


def test_full_party_and_decline_do_not_collect():
    for full,choices in [(True,[0,0]),(False,[0,1])]:
        vm=QuestVM();reach_corner(vm)
        if full:vm.party=6
        vm.event('scyther_corner',choices)
        assert not any(p['kind']=='collect' for p in vm.presentations) and vm.gifts==0


def test_house_exit_turns_to_the_door_before_hiding():
    vm=QuestVM();vm.event('house_entry')
    tiana=vm.defs['sequence']['tiana_house']['npc_id']
    leave=next(p for p in vm.presentations if p['kind']=='hide' and p['actor']==tiana)
    assert leave['positions'][tiana]==(4,7) and leave['facings'][tiana]==1
