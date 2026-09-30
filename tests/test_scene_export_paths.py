"""Execute every gathered approach in the delivered scripts, including initial gaze.

Bytecode VM + real ROM/map paths; follower geometry is checked by Project.
This does not emulate graphics, camera, input scheduling or the native effect.
"""
import copy
from pathlib import Path
import pytest
from sovereign_editor.core import Project
from sovereign_editor import story_authoring as st,scene_authoring as sa,dialogue_format as fmt
from sovereign_editor.formats import resource
from tests.test_scyther_quest import QuestVM,STATE,MEDICINE

ROOT=Path(__file__).resolve().parents[1]
PROJECT=ROOT/'projects/scyther-orchestration-1'
PAIR=PROJECT/'exports/scyther-r34/game.nds'
pytestmark=pytest.mark.skipif(not PAIR.exists(),reason='r34 local acceptance pair absent')

@pytest.fixture(scope='module')
def exported():
    before=(PROJECT/'project.json').read_bytes();p=Project(PROJECT);state=p.composed()
    definitions={k:copy.deepcopy(st.catalog(state,k)) for k in ('sequence','state','trainer')}
    for spec in definitions['sequence'].values():spec['header']=spec['context']['header']
    yield p,state,definitions,PAIR.read_bytes(),st.allocation(p,state)
    assert (PROJECT/'project.json').read_bytes()==before


@pytest.mark.parametrize('key,stage,final_stage,final_facing',[
    ('house_entry',0,1,1),('beach_find',1,2,2),('beach_flee',2,3,3),
    ('town_flee',3,4,3),('road_flee',4,5,3),('corner_find',5,6,0),
    ('scyther_corner',7,8,3)])
def test_all_real_approaches_execute_the_exported_script_to_the_authored_pose(exported,key,stage,final_stage,final_facing):
    p,state,defs,rom,allocation=exported;s=defs['sequence'][key]
    raw=resource(rom,fmt.SCRIPT_ARCHIVE,s['script_member'])[1]
    entries=fmt.script_entries(raw)[1];start=entries[allocation[key][0]-1]
    end=min([i for i in entries if i>start]+[len(raw)]);code=raw[start:end]
    gathers=sa.gather_routes(p,state,s);paths=next(iter(gathers.values()));assert paths
    for origin in paths:
        for facing in range(4):
            vm=QuestVM();vm.defs=copy.deepcopy(defs);vm.set(STATE,stage)
            if stage==7:vm.set(MEDICINE,1)
            vm.load(s['header']);vm.current_event=key
            vm.positions[255]=origin;vm.facings[255]=facing
            vm.last_actor=s['npc_id'] if s['kind']=='npc' else None
            vm.execute(code,[0,0])
            assert vm.get(STATE)==final_stage,(key,origin,facing,vm.get(STATE))
            assert vm.facings[255]==final_facing,(key,origin,facing,vm.facings)
            assert not vm.locked and not vm.follower_free
            if stage==7:
                assert vm.gifts==1 and len([x for x in vm.presentations if x['kind']=='collect'])==1
            # Conditional triggers are quiet after advancing, at this same tile.
            if s.get('trigger'):
                pages=len(vm.presentations);vm.execute(code)
                assert len(vm.presentations)==pages and vm.get(STATE)==final_stage
