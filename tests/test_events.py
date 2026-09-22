"""General event transactions, independent archive readback and native UI staging."""
import copy
import json
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import ndspy.narc
import ndspy.rom
import pytest

from sovereign_editor import event_authoring, world
from sovereign_editor.core import Project, atomic_json
from sovereign_editor.formats import EditorError, resource

CG = dict(header=67, cell=[17, 12])
NB = dict(header=60, cell=[21, 12])
LAB = dict(header=61, cell=[0, 0])
ROUTE = dict(header=33, cell=[18, 12])


@pytest.fixture(scope='module')
def workspace(tmp_path_factory):
    source = Path('projects/map-scenery-1')
    if not (source/'baseline.nds').exists():
        pytest.skip('Qualified local ROM required')
    root = tmp_path_factory.mktemp('events')/'project'
    root.mkdir()
    for name in ('baseline.nds', 'project.json'):
        shutil.copy2(source/name, root/name)
    return root, json.loads((root/'project.json').read_text())


@pytest.fixture
def project(workspace):
    root, original = workspace
    atomic_json(root/'project.json', original)
    yield Project(root)
    atomic_json(root/'project.json', original)


def apply(p, kind='npc', event_id=2, values=None, context=CG, **kw):
    return p.apply_event_edit(p.doc['revision'], **context, kind=kind, event_id=event_id,
                              values=values if values is not None else dict(x=562,z=408,facing=1,range_x=0), **kw)


def event(p, kind='npc', event_id=2, context=CG):
    return next(r for r in p.map_events(**context)['events'] if (r['kind'],r['id'])==(kind,event_id))


def archive(rom, member):
    return ndspy.narc.NARC(rom.getFileByName(world.EVENT_ARCHIVE)).files[member]


def test_composed_views_across_maps_and_bindings(project):
    assert event(project,'background',2,NB)['x']==684
    assert event(project,'npc',1)['x']==555  # Accepted legacy NPC move is composed.
    assert event(project,'background',2,NB)['association']['object_id']=='baseline:0:13'
    assert event(project,'npc',2)['association']['sprite']==325
    assert event(project,'background',0,ROUTE)['in_cell']
    assert not event(project,'npc',0,ROUTE)['in_cell']
    assert event(project,'warp',0,LAB)['connection']['returns_to_source']
    assert event(project,'background',0,LAB)['background_type']==0


def test_noop_preview_save_reopen_and_undo(project):
    before=project.path.read_bytes()
    plan=project.plan_event_edit(**CG,kind='npc',event_id=2,values={'x':562,'z':408})
    assert not plan['empty'] and project.path.read_bytes()==before
    r=apply(project,values={})
    assert not r['changed'] and project.path.read_bytes()==before
    r=apply(project)
    assert r['revision']==14 and len(project.doc['history'])==12
    p=Project(project.root)
    assert [event(p)[k] for k in ('x','z','facing','range_x')]==[562,408,1,0]
    assert event(p)['movement']==5 and event(p)['script']==9
    unchanged=p.path.read_bytes()
    assert not apply(p,values=dict(x=562,z=408,facing=1,range_x=0))['changed']
    assert unchanged==p.path.read_bytes()
    p.undo(14)
    assert p.doc['map_edits']==json.loads(before)['map_edits']
    assert event(p)['x']==561


def test_export_combined_exact_preservation_and_net_zero(project,tmp_path):
    accepted=Path('projects/map-scenery-1/exports/scenery-r13/game.nds').read_bytes()
    baseline=ndspy.rom.NintendoDSRom(accepted)
    apply(project)
    apply(project,'background',1,dict(x=553,z=396))
    apply(project,'warp',3,dict(destination=72,destination_warp=0),reciprocal=True)
    apply(project,'warp',4,dict(destination=71,destination_warp=0),reciprocal=True)
    out=tmp_path/'events'
    report=project.export(out,project.doc['revision'])
    actual_bytes=(out/'game.nds').read_bytes()
    actual=ndspy.rom.NintendoDSRom(actual_bytes)
    expected=bytearray(accepted)
    # Independently calculate the expected fixed offsets from count-prefixed sections.
    for member in [64,68,69]:
        old=archive(baseline,member)
        new=archive(actual,member)
        expected_event=bytearray(old)
        bg=struct.unpack_from('<I',old,0)[0]
        npc_start=8+bg*20
        npc_count=struct.unpack_from('<I',old,4+bg*20)[0]
        warp_start=npc_start+npc_count*32+4
        if member==64:
            struct.pack_into('<i',expected_event,4+20+4,553)
            struct.pack_into('<h',expected_event,npc_start+2*32+12,1)
            struct.pack_into('<h',expected_event,npc_start+2*32+20,0)
            struct.pack_into('<2H',expected_event,npc_start+2*32+24,562,408)
            struct.pack_into('<H',expected_event,warp_start+3*12+4,72)
            struct.pack_into('<H',expected_event,warp_start+4*12+4,71)
        elif member==68:
            struct.pack_into('<H',expected_event,warp_start+6,4)
        elif member==69:
            struct.pack_into('<H',expected_event,warp_start+6,3)
        assert new==expected_event
        # Search verified unique complete member bytes in the accepted ROM, independently of product offsets.
        pos=accepted.find(old)
        assert pos>=0 and accepted.find(old,pos+1)==-1
        expected[pos:pos+len(old)]=expected_event
    assert actual_bytes==expected  # Including every non-event byte and all resized map resources.
    assert report['native_acceptance']=='pending'
    # Reversing values leaves transactions but restores exact accepted r13 bytes.
    apply(project,values=dict(x=561,z=407,facing=3,range_x=1))
    apply(project,'background',1,dict(x=554,z=396))
    apply(project,'warp',3,dict(destination=71,destination_warp=0),reciprocal=True)
    apply(project,'warp',4,dict(destination=72,destination_warp=0),reciprocal=True)
    project.export(tmp_path/'restored',project.doc['revision'])
    assert (tmp_path/'restored/game.nds').read_bytes()==accepted


def test_warp_pair_is_atomic_navigable_and_undoable(project):
    source=project.path.read_bytes()
    r=apply(project,'warp',3,dict(destination=72,destination_warp=0),reciprocal=True)
    assert len(r['preview']['events'])==2
    assert event(project,'warp',3)['connection']['returns_to_source']
    assert event(project,'warp',0,dict(header=72,cell=[0,0]))['destination_warp']==3
    assert Project(project.root).doc['revision']==14
    project.undo(14)
    assert project.doc['map_edits']==json.loads(source)['map_edits']
    assert event(project,'warp',3)['destination']==71
    assert event(project,'warp',0,dict(header=72,cell=[0,0]))['destination_warp']==4


@pytest.mark.parametrize('kind,event_id,values,context,code',[
    ('npc',2,{'x':560},CG,'BLOCKED_TILE'),
    ('npc',2,{'x':576},CG,'OUTSIDE_MAP'),
    ('npc',2,{'facing':4},CG,'INVALID_INPUT'),
    ('npc',2,{'range_x':32},CG,'INVALID_INPUT'),
    ('npc',2,{'script':10},CG,'UNSUPPORTED_EDIT'),
    ('background',1,{'x':556,'z':401},CG,'EVENT_CONFLICT'),
    ('warp',3,{'destination':65535},CG,'NOT_FOUND'),
    ('warp',3,{'destination_warp':999},CG,'NOT_FOUND'),
    ('npc',999,{},CG,'NOT_FOUND'),
    ('npc',0,{'x':600},ROUTE,'OUTSIDE_MAP'),
    ('npc',2,{'x':True},CG,'INVALID_INPUT'),
])
def test_invalid_edits_refuse_without_writes(project,kind,event_id,values,context,code):
    before=project.path.read_bytes()
    with pytest.raises(EditorError) as exc:
        apply(project,kind,event_id,values,context)
    assert exc.value.code==code and project.path.read_bytes()==before

@pytest.mark.parametrize('tamper',['before','after','offset','identity','dependency','index','unknown-field','reorder'])
def test_tampered_transactions_refuse(project,tamper):
    apply(project)
    apply(project,values={'facing':2})
    doc=copy.deepcopy(project.doc)
    t=doc['map_edits'][-1]
    if tamper in ('before','after'):t['changes'][0][tamper]='00'*32
    elif tamper=='offset':t['changes'][0]['offset']+=32
    elif tamper=='identity':t['changes'][0]['id']=0
    elif tamper=='dependency':t['dependencies'][0]['resources'][0]['sha256']='0'*64
    elif tamper=='index':t['index']=0
    elif tamper=='unknown-field':t['request']['values']['script']=1
    else:doc['map_edits'][-2:]=reversed(doc['map_edits'][-2:])
    atomic_json(project.path,doc)
    with pytest.raises(EditorError):Project(project.root)


def test_stale_legacy_and_later_collision_conflicts(project):
    apply(project, event_id=1,values={'facing':1})
    before=project.path.read_bytes()
    with pytest.raises(EditorError) as exc:project.apply_event_edit(13,**CG,kind='npc',event_id=1,values={'x':556})
    assert exc.value.code=='STALE_REVISION'
    with pytest.raises(EditorError) as exc:project.move_npc(1,556,399,14)
    assert exc.value.code=='EVENT_CONFLICT'
    with pytest.raises(EditorError) as exc:
        project.apply_map_edit(14,**CG,permissions=[dict(x=555,z=399,after='0080')])
    assert exc.value.code=='BLOCKED_TILE'
    assert project.path.read_bytes()==before


def test_sign_and_general_event_edits_compose_both_orders(project):
    apply(project,'background',2,{'x':683},NB)
    assert project.map_sign(**NB)['position']==[683,400]
    project.apply_map_edit(project.doc['revision'],**NB,align_sign=True)
    assert event(project,'background',2,NB)['x']==684
    apply(project,'background',2,{'x':683},NB)
    assert Project(project.root).map_sign(**NB)['position']==[683,400]
    project.undo(project.doc['revision'])
    assert event(project,'background',2,NB)['x']==684


def test_interior_route_and_warp_position(project):
    apply(project,'background',0,{'z':9},LAB)
    apply(project,'npc',2,{'facing':2},ROUTE)
    apply(project,'warp',0,{'x':5},LAB)
    assert event(Project(project.root),'warp',0,LAB)['x']==5
    assert event(project,'background',0,LAB)['z']==9


def test_cli_shares_preview_write_and_errors(project):
    prefix=[sys.executable,'-m','sovereign_editor.cli','event-edit','--project',str(project.root),
            '--header','67','--cell','17,12','--kind','npc','--id','2','--x','562','--z','408','--range-x','0']
    before=project.path.read_bytes()
    preview=subprocess.run(prefix+['--dry-run'],capture_output=True,text=True)
    assert preview.returncode==0 and not json.loads(preview.stdout)['result']['empty']
    assert project.path.read_bytes()==before
    saved=subprocess.run(prefix+['--revision','13'],capture_output=True,text=True)
    assert saved.returncode==0 and json.loads(saved.stdout)['result']['revision']==14
    stale=subprocess.run(prefix+['--revision','13'],capture_output=True,text=True)
    assert stale.returncode==2 and json.loads(stale.stdout)['error']['code']=='STALE_REVISION'


def test_record_padding_and_signed_values_are_preserved():
    # Independently constructed background with nonzero padding; negative facing/range stay readable.
    background=struct.pack('<HHiiiHH',15,1,685,400,-3,4,0xA55A)
    npc=bytearray(32)
    struct.pack_into('<h',npc,12,-1)
    struct.pack_into('<h',npc,20,-1)
    raw=struct.pack('<I',1)+background+struct.pack('<I',1)+npc+struct.pack('<II',0,0)
    decoded=event_authoring.records(raw)
    assert decoded[0]['direction']==4 and decoded[0]['raw']==background
    assert decoded[1]['facing']==-1 and decoded[1]['range_x']==-1


def test_event_export_without_scenery_and_exact_undo(project,tmp_path):
    doc=copy.deepcopy(project.doc)
    doc.update(revision=0,map_edits=[],positions={},placement_moves={},history=[])
    atomic_json(project.path,doc)
    p=Project(project.root)
    apply(p,'background',0,{'x':580},ROUTE)
    p.export(tmp_path/'plain-event',1)
    actual=(tmp_path/'plain-event/game.nds').read_bytes()
    base=ndspy.rom.NintendoDSRom(p.blob)
    member=archive(base,30)
    offset=p.blob.find(member)
    assert offset>=0 and p.blob.find(member,offset+1)==-1
    expected=bytearray(p.blob)
    struct.pack_into('<i',expected,offset+8,580)
    assert actual==expected
    p.undo(1)
    p.export(tmp_path/'plain-undo',2)
    assert (tmp_path/'plain-undo/game.nds').read_bytes()==p.blob


def test_facing_only_preserves_stock_range_intersections(project):
    # Stock resident range touches a blocker; unrelated facing edits and restoration
    # must preserve those existing intersections without granting new ones.
    apply(project,values={'facing':1})
    assert event(project)['x']==561 and event(project)['range_x']==1
    apply(project,values={'facing':3})
    assert event(project)['facing']==3
    with pytest.raises(EditorError):
        project.plan_event_edit(**CG,kind='npc',event_id=2,values=[])


def test_composed_legacy_npc_vacated_tile_can_be_authored(project):
    apply(project,event_id=1,values=dict(x=556,z=399,range_z=0))
    project.apply_map_edit(14,**CG,permissions=[dict(x=555,z=399,after='0080')])
    p=Project(project.root)
    assert event(p,'npc',1)['x']==556
    assert not next(n for n in p.scene()['npcs'] if n['id']==1)['editable']
    assert p.permission_cells(**CG,x=555,z=399)['cells'][0]['blocked']
    p.undo(15);p.undo(16)
    assert event(p,'npc',1)['x']==555


def test_legacy_move_cannot_save_unreopenable_overlap(project):
    apply(project,values=dict(x=563,z=408,range_x=0))
    before=project.path.read_bytes()
    with pytest.raises(EditorError) as exc:project.move_npc(1,563,408,14)
    assert exc.value.code=='EVENT_CONFLICT' and project.path.read_bytes()==before
    assert Project(project.root).doc['revision']==14


def test_legacy_move_composes_when_disjoint(project):
    apply(project,values=dict(x=563,z=408,range_x=0))
    project.move_npc(1,555,398,14)
    p=Project(project.root)
    assert (event(p,'npc',1)['x'],event(p,'npc',1)['z'])==(555,398)
    assert event(p,'npc',2)['x']==563


@pytest.mark.parametrize('positions',[{'bad':{}},{'1':{'x':'bad','z':399}}, {'1':{'x':True,'z':399}},
                                      {'1':{'x':-1,'z':399}}, {'1':[]}, []])
def test_malformed_legacy_state_refuses_before_event_composition(positions):
    class Stub:
        base_events=[]
    with pytest.raises(EditorError):event_authoring.initialise(Stub(),{},positions)
