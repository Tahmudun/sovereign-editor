"""Scene occupancy is temporal and must include stock residents and entrances."""
import copy
import pytest
from sovereign_editor import scene_authoring as sc
from sovereign_editor.formats import EditorError


class FlatProject:
    def context(self,header,cell):return {'header':{'id':header},'map_member':0,'origin':[0,0]}
    def member_raw(self,member):return b'\0\0'


def setup(monkeypatch,nodes,rows=(),actors=None):
    actors=actors or {'a':dict(kind='npc',x=2,z=2,npc_id=1,event_member=1,range_x=0,range_z=0)}
    s=dict(kind='trigger',x=0,z=0,context={'header':1,'cell':[0,0]},event_member=1,
           trigger={'state':'stage','value':0,'width':1,'height':1},nodes=nodes)
    state={'story':{'sequence':actors},'permissions':{}}
    monkeypatch.setattr(sc,'floor_height',lambda *a:0)
    monkeypatch.setattr(sc.world,'cell_offset',lambda *a:0)
    monkeypatch.setattr(sc.ev,'raw_member',lambda *a:b'')
    monkeypatch.setattr(sc.ev,'records',lambda *a:list(rows))
    return FlatProject(),state,s


def move(id,path,next='advance',actor='a'):
    return dict(id=id,op='move',path=path,actor=actor,speed='walk',next=next)


def finish():
    return [dict(id='advance',op='set',state='stage',value=1,next='end'),dict(id='end',op='end')]


def test_next_route_uses_previous_endpoint(monkeypatch):
    args=setup(monkeypatch,[move('first',[[2,2],[3,2]],'second'),move('second',[[3,2],[3,3]]),*finish()])
    sc.validate_routes(*args)
    args[2]['nodes'][1]['path'][0]=[2,2]
    with pytest.raises(EditorError,match='position'):sc.validate_routes(*args)


def test_vacated_actor_tile_is_available_but_new_position_blocks(monkeypatch):
    actors={k:dict(kind='npc',x=x,z=2,npc_id=i,event_member=1) for k,x,i in [('a',2,1),('b',4,2)]}
    args=setup(monkeypatch,[move('a',[[2,2],[2,3]],'b'),move('b',[[4,2],[2,2]],actor='b'),*finish()],actors=actors)
    sc.validate_routes(*args)
    args[2]['nodes'][1]['path'].append([2,3])
    with pytest.raises(EditorError,match='actor or entrance'):sc.validate_routes(*args)


@pytest.mark.parametrize('row',[
    dict(kind='npc',id=9,x=4,z=2,range_x=1,range_z=0),
    dict(kind='warp',id=0,x=3,z=1)])
def test_stock_roaming_range_and_door_approach_refused(monkeypatch,row):
    args=setup(monkeypatch,[move('a',[[2,2],[3,2]]),*finish()],[row])
    with pytest.raises(EditorError,match='actor or entrance'):sc.validate_routes(*args)


def test_player_tile_blocks_npc_route(monkeypatch):
    args=setup(monkeypatch,[move('a',[[2,2],[0,2],[0,0]]),*finish()])
    with pytest.raises(EditorError,match='actor or entrance'):sc.validate_routes(*args)


def test_mutually_exclusive_actor_does_not_block(monkeypatch):
    actors={'a':dict(kind='npc',x=2,z=2,npc_id=1,event_member=1),
            'b':dict(kind='npc',x=3,z=2,npc_id=2,event_member=1,presence={'state':'stage','values':[1]})}
    args=setup(monkeypatch,[move('a',[[2,2],[3,2]]),*finish()],actors=actors)
    sc.validate_routes(*args)
    args[2]['nodes'][-1:]=[dict(id='end',op='sync',next='done'),dict(id='done',op='end')]
    with pytest.raises(EditorError,match='Shown actor'):sc.validate_routes(*args)


def test_conditional_trigger_must_advance_every_exit(monkeypatch):
    args=setup(monkeypatch,[move('a',[[2,2],[3,2]],'end'),dict(id='end',op='end')])
    with pytest.raises(EditorError,match='advance'):sc.validate_routes(*args)


def test_unavailable_actor_rejected(monkeypatch):
    actors={'a':dict(kind='npc',x=2,z=2,npc_id=1,event_member=1,presence={'state':'stage','values':[1]})}
    args=setup(monkeypatch,[move('a',[[2,2],[3,2]]),*finish()],actors=actors)
    with pytest.raises(EditorError,match='absent'):sc.validate_routes(*args)


def test_trigger_rectangles_require_disjoint_stage_conditions(monkeypatch):
    from sovereign_editor import story_authoring
    s=dict(kind='trigger',x=1,z=1,event_member=1,trigger={'state':'stage','value':0,'width':3,'height':3})
    other={**copy.deepcopy(s),'x':3}
    state={'story':{'sequence':{'a':s,'b':other}}}
    monkeypatch.setattr(story_authoring,'allocation',lambda *a:{'a':(5,1),'b':(6,1)})
    monkeypatch.setattr(sc.ev,'raw_member',lambda *a:b'')
    monkeypatch.setattr(sc.ev,'records',lambda *a:[dict(kind='trigger',id=0,script=5,x=1,z=1,width=3,height=3),dict(kind='trigger',id=1,script=6,x=3,z=1,width=3,height=3)])
    with pytest.raises(EditorError,match='overlaps'):sc.validate_triggers(None,state)
    other['trigger']['value']=1;sc.validate_triggers(None,state)
    other['trigger']['state']='unrelated'
    with pytest.raises(EditorError,match='overlaps'):sc.validate_triggers(None,state)
