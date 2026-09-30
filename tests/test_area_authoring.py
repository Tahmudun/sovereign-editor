"""Bounded area authoring: real pinned resources, independent binary readback."""
import copy
import json
import shutil
import struct
from pathlib import Path
import ndspy.rom
import ndspy.narc
import pytest
from sovereign_editor.core import Project,atomic_json
from sovereign_editor.formats import EditorError,resource,map_data
from sovereign_editor import dialogue_format as fmt,world,area_layout,surface_authoring
from historical import exported

CG=dict(header=67,cell=[17,12])

@pytest.fixture(scope='module')
def workspace(tmp_path_factory):
    root=tmp_path_factory.mktemp('area')/'project';root.mkdir()
    source=Path('projects/map-events-1')
    for name in ('baseline.nds','project.json'):shutil.copy2(source/name,root/name)
    return root,json.loads((root/'project.json').read_text())

@pytest.fixture
def p(workspace):
    root,doc=workspace;atomic_json(root/'project.json',doc)
    yield Project(root)
    atomic_json(root/'project.json',doc)


def op(kind,request,context=CG):return dict(kind=kind,context=context,request=request)
def npc(**kw):return op('interaction',dict(action='create',kind='npc',donor_id=2,x=554,z=404,dialogue='The path is open.\nEnjoy your visit!',**kw))
def surface(material='road01'):return op('surface',dict(x=557,z=403,width=1,height=1,material=material))
def apply(p,operations):return p.apply_area_edit(p.doc['revision'],operations=operations)
def narc(rom,path,member):return ndspy.narc.NARC(rom.getFileByName(path)).files[member]


def test_surface_and_dialogue_preview_save_reopen_atomic_undo(p):
    original=p.path.read_bytes();plan=p.plan_area_edit([surface(),npc()])
    assert len(plan['transactions'])==2 and p.path.read_bytes()==original
    preview=p.area_preview_project(plan)
    assert len(preview.simple_interactions(**CG))==1 and not p.simple_interactions(**CG)
    a=surface_authoring.model(preview,preview.context(**CG),preview.composed())
    assert a!=map_data(p.member_raw(5))[2]
    apply(p,[surface(),npc()]);reopened=Project(p.root)
    assert reopened.doc['revision']==19 and len(reopened.doc['history'])==17
    assert surface_authoring.model(reopened,reopened.context(**CG),reopened.composed())==a
    assert reopened.simple_interactions(**CG)[0]['dialogue']=='The path is open.\nEnjoy your visit!'
    reopened.undo(19)
    assert reopened.doc['map_edits']==json.loads(original)['map_edits']
    assert not reopened.simple_interactions(**CG)


def test_export_independent_message_script_event_model_and_netzero(p,tmp_path):
    accepted=exported('projects/map-events-1/exports/events-r18/game.nds')
    prior=ndspy.rom.NintendoDSRom(accepted)
    apply(p,[surface(),npc()]);identity=p.simple_interactions(**CG)[0]['identity']
    out=tmp_path/'authored';p.export(out,p.doc['revision']);rom=ndspy.rom.NintendoDSRom((out/'game.nds').read_bytes())
    h=p.context(**CG)['header'];old_event=narc(prior,world.EVENT_ARCHIVE,h['event_file']);new_event=narc(rom,world.EVENT_ARCHIVE,h['event_file'])
    nb=struct.unpack_from('<I',old_event)[0];old_npc=4+nb*20;count=struct.unpack_from('<I',old_event,old_npc)[0]
    assert struct.unpack_from('<I',new_event,old_npc)[0]==count+1
    assert new_event[:old_npc]==old_event[:old_npc]
    assert new_event[old_npc+4:old_npc+4+count*32]==old_event[old_npc+4:old_npc+4+count*32]
    assert new_event[old_npc+4+(count+1)*32:]==old_event[old_npc+4+count*32:]
    added=new_event[old_npc+4+count*32:old_npc+4+(count+1)*32]
    assert struct.unpack_from('<H',added,8)[0]==0 and struct.unpack_from('<2H',added,24)==(554,404)
    old_text=narc(prior,fmt.TEXT_ARCHIVE,h['text_archive']);new_text=narc(rom,fmt.TEXT_ARCHIVE,h['text_archive'])
    count,key=struct.unpack_from('<2H',old_text);assert struct.unpack_from('<H',new_text)[0]==count+1
    for i in range(count+1):
        seed=(key*765*(i+1))&65535;mask=seed|seed<<16
        pos,length=[x^mask for x in struct.unpack_from('<2I',new_text,4+i*8)]
        if i<count:
            op_,ol=[x^mask for x in struct.unpack_from('<2I',old_text,4+i*8)]
            assert ol==length and new_text[pos:pos+2*length]==old_text[op_:op_+2*ol]
        else:
            chars=[v^(((i+1)*596947+j*18749)&65535) for j,(v,) in enumerate(struct.iter_unpack('<H',new_text[pos:pos+2*length]))]
            assert chars==[fmt.CHARS[c] for c in 'The path is open.\nEnjoy your visit!']+[65535]
    old_script=narc(prior,fmt.SCRIPT_ARCHIVE,h['script_file']);new_script=narc(rom,fmt.SCRIPT_ARCHIVE,h['script_file'])
    cursor=0
    while old_script[cursor:cursor+2]!=b'\x13\xfd':
        assert struct.unpack_from('<I',new_script,cursor)[0]==struct.unpack_from('<I',old_script,cursor)[0]+4
        cursor+=4
    assert new_script[cursor+4:cursor+6]==b'\x13\xfd'
    assert new_script[cursor+6:cursor+6+len(old_script)-cursor-2]==old_script[cursor+2:]
    expected=struct.pack('<4HHB4H',73,1500,96,104,45,count,50,53,97,2)
    assert new_script.endswith(expected)
    assert struct.unpack_from('<H',added,10)[0]==cursor//4+1
    for fid,(before,after) in enumerate(zip(prior.files,rom.files)):
        name=prior.filenames.filenameOf(fid)
        if name not in (world.MAP_ARCHIVE,world.EVENT_ARCHIVE,fmt.TEXT_ARCHIVE,fmt.SCRIPT_ARCHIVE):assert after==before
    for archive in (world.MAP_ARCHIVE,world.EVENT_ARCHIVE,fmt.TEXT_ARCHIVE,fmt.SCRIPT_ARCHIVE):
        a=ndspy.narc.NARC(prior.getFileByName(archive)).files;b=ndspy.narc.NARC(rom.getFileByName(archive)).files
        changed={world.MAP_ARCHIVE:5,world.EVENT_ARCHIVE:h['event_file'],fmt.TEXT_ARCHIVE:h['text_archive'],fmt.SCRIPT_ARCHIVE:h['script_file']}[archive]
        assert all(x==y for i,(x,y) in enumerate(zip(a,b)) if i!=changed)
    apply(p,[surface(None),op('interaction',dict(action='delete',identity=identity))])
    p.export(tmp_path/'netzero',p.doc['revision'])
    assert (tmp_path/'netzero/game.nds').read_bytes()==accepted


def test_stale_and_invalid_batch_never_writes(p):
    before=p.path.read_bytes()
    with pytest.raises(EditorError):apply(p,[surface(),op('interaction',dict(action='create',kind='npc',donor_id=2,x=565,z=411,dialogue='Hi'))])
    assert p.path.read_bytes()==before
    with pytest.raises(EditorError) as exc:p.apply_area_edit(0,operations=[surface()])
    assert exc.value.code=='STALE_REVISION' and p.path.read_bytes()==before


@pytest.mark.parametrize('kind',['before','dependency','index','text'])
def test_transaction_tampering_refuses(p,kind):
    apply(p,[npc()]);doc=copy.deepcopy(p.doc);t=doc['map_edits'][-1]
    if kind=='before':t['after']['x']+=1
    elif kind=='dependency':t['dependencies']['text']='0'*64
    elif kind=='index':t['index']=0
    else:t['request']['dialogue']='Changed'
    atomic_json(p.path,doc)
    with pytest.raises(EditorError):Project(p.root)


def test_background_growth_composes_existing_event_edits(p,tmp_path):
    apply(p,[op('interaction',dict(action='create',kind='background',donor_id=1,x=557,z=403,dialogue='A quiet corner.'))])
    p.apply_event_edit(p.doc['revision'],**CG,kind='warp',event_id=3,values={'x':561})
    p.apply_event_edit(p.doc['revision'],**CG,kind='npc',event_id=2,values={'facing':2})
    reopened=Project(p.root);events=reopened.map_events(**CG)['events']
    assert next(e for e in events if e['kind']=='warp' and e['id']==3)['x']==561
    assert next(e for e in events if e['kind']=='npc' and e['id']==2)['facing']==2
    reopened.export(tmp_path/'sign-plus-events',reopened.doc['revision'])


def test_surface_noop_and_unsupported_overlay(p):
    before=p.path.read_bytes();apply(p,[op('surface',dict(x=554,z=404,width=1,height=1,material='road01'))])
    assert p.path.read_bytes()==before
    with pytest.raises(EditorError) as exc:apply(p,[op('surface',dict(x=554,z=398,width=1,height=1,material='road01'))])
    assert exc.value.code=='UNSUPPORTED_SURFACE' and p.path.read_bytes()==before


def test_group_copy_transfer_and_atomic_undo(p):
    before=p.path.read_bytes();ctx=p.context(**CG)
    ops=area_layout.group(p,ctx,[8,9],'move',0,-2,events=[dict(kind='warp',event_id=3)])
    apply(p,ops);q=Project(p.root)
    model=next(m for m in q.map_view(**CG,include_grid=False)['placements'] if m['slot']==8)
    assert model['position']['z']==398.5
    warp=next(e for e in q.map_events(**CG)['events'] if e['kind']=='warp' and e['id']==3)
    assert (warp['x'],warp['z'])==(558,399) and warp['connection']['returns_to_source']
    q.undo(q.doc['revision']);assert q.doc['map_edits']==json.loads(before)['map_edits']
    route=dict(header=33,cell=[18,12]);before_count=len(q.map_view(**route,include_grid=False)['placements'])
    operations=area_layout.group(q,q.context(**route),[0],'duplicate',3,1)
    apply(q,operations);assert len(q.map_view(**route,include_grid=False)['placements'])==before_count+1


def test_cross_map_library_import_height_guard_and_undo(p):
    ctx=p.context(**CG);sources=area_layout.library_sources(p,ctx)
    assert any(s['header']==33 and s['cell']==[18,12] for s in sources)
    source=dict(header=33,cell=[18,12]);entries=area_layout.templates(p,ctx,source,search='sign')
    assert entries and entries[0]['slot']==0
    operation=op('scenery',dict(operation='import',slot=0,x=554.5,z=403.5,destination=CG),source)
    before=p.path.read_bytes();apply(p,[operation]);assert p.doc['revision']==19
    q=Project(p.root);assert any(v['model_id']==32 for v in q.map_view(**CG,include_grid=False)['placements'])
    q.undo(q.doc['revision']);assert q.doc['map_edits']==json.loads(before)['map_edits']


def test_interaction_edit_duplicate_remove_and_preserved_stock(p):
    apply(p,[npc()]);identity=p.simple_interactions(**CG)[0]['identity']
    apply(p,[op('interaction',dict(action='edit',identity=identity,dialogue='Welcome!',x=555,z=404,facing=2))])
    assert p.simple_interactions(**CG)[0]['dialogue']=='Welcome!'
    apply(p,[op('interaction',dict(action='duplicate',identity=identity,x=557,z=403))])
    records=p.simple_interactions(**CG);assert len(records)==2 and len({r['npc_id'] for r in records})==2
    apply(p,[op('interaction',dict(action='delete',identity=identity))])
    assert len(Project(p.root).simple_interactions(**CG))==1
    with pytest.raises(EditorError):apply(p,[op('interaction',dict(action='delete',identity='baseline:64:2'))])


@pytest.mark.parametrize('text',['a'*29,'a\nb\nc','Hi 🐢','   '])
def test_dialogue_text_limits(p,text):
    if text.isspace():
        # Whitespace-only text is addressed by the plain-dialogue validation.
        with pytest.raises(EditorError):fmt.encode_message(text)
    else:
        with pytest.raises(EditorError):fmt.encode_message(text)


def test_group_collision_union_and_linked_return(p):
    ctx=p.context(**CG)
    selected=[dict(x=x,z=z) for x in range(557,561) for z in range(397,402)]
    ops=area_layout.group(p,ctx,[8,9],'move',0,-2,cells=selected,events=[dict(kind='warp',event_id=3)])
    apply(p,ops)
    cells=p.permission_cells(**CG,x=558,z=399,width=1,height=3)['cells']
    raw=p.member_raw(5);state=p.composed()
    def pair(z):
        offset=world.cell_offset(ctx,558,z)
        return state['permissions'].get((5,offset),raw[offset:offset+2])
    assert pair(399)==b'\x69\x80' and pair(401)==b'\x00\x00'
    for z in range(395,402):
        cells=p.permission_cells(**CG,x=557,z=z,width=4,height=1)['cells']
        assert all(c['blocked']==(z<=399) for c in cells)
    p.undo(p.doc['revision'])
    assert all(c['blocked'] for c in p.permission_cells(**CG,x=557,z=401,width=4,height=1)['cells'])


def test_linked_door_moves_behavior_without_extra_collision_selection(p):
    ctx=p.context(**CG);before=p.path.read_bytes()
    ops=area_layout.group(p,ctx,[8,9],'move',0,-2,events=[dict(kind='warp',event_id=3)])
    apply(p,ops);state=p.composed();raw=p.member_raw(5)
    for z,expected in [(399,b'\x69\x80'),(401,b'\x00\x00')]:
        offset=world.cell_offset(ctx,558,z)
        assert state['permissions'].get((5,offset),raw[offset:offset+2])==expected
    p.undo(p.doc['revision']);assert p.doc['map_edits']==json.loads(before)['map_edits']
    apply(p,[op('map',dict(permissions=[dict(x=558,z=399,before='0080',after='0280')]))])
    before=p.path.read_bytes()
    with pytest.raises(EditorError) as exc:
        area_layout.group(p,ctx,[8,9],'move',0,-2,events=[dict(kind='warp',event_id=3)])
    assert exc.value.code=='UNSUPPORTED_ENTRANCE'
    assert p.path.read_bytes()==before


def test_project_clone_preserves_source_and_history(p,tmp_path):
    before=p.path.read_bytes();q=p.clone(tmp_path/'clone','Area work')
    assert p.path.read_bytes()==before and q.blob==p.blob
    assert q.doc['map_edits']==p.doc['map_edits'] and q.doc['history']==p.doc['history']
    apply(q,[surface()]);assert p.path.read_bytes()==before


def test_surface_final_model_compacts_to_native_budget(p):
    operation=op('surface',dict(x=557,z=402,width=4,height=2,material='road01'))
    plan=p.plan_area_edit([operation]);trial=p.area_preview_project(plan)
    model=surface_authoring.model(trial,p.context(**CG),trial.composed())
    assert 24612<len(model)<0xf000
    # The same eight-tile patch used to accumulate >135 KB of abandoned lists.
    assert len(model)<35048  # Native quads retain the original topology with fewer vertices.


def test_scenery_capacity_refuses_before_partial_save(p):
    from sovereign_editor import scenery
    ctx=p.context(header=33,cell=[18,12]);state=copy.deepcopy(p.composed());table=scenery.table_for(p,ctx,state)
    donor=copy.deepcopy(table[0])
    for slot in range(100,130):table[slot]=copy.deepcopy(donor)
    before=p.path.read_bytes()
    with pytest.raises(EditorError) as exc:scenery.plan(p,ctx,state,99,'duplicate',0,x=584.5,z=399.5)
    assert exc.value.code=='RESOURCE_CAPACITY' and p.path.read_bytes()==before


def test_multi_object_group_transfer_delete_and_undo(p):
    from sovereign_editor import scenery
    source=p.context(**CG);destination=dict(header=33,cell=[18,12]);target=p.context(**destination)
    before=copy.deepcopy(p.doc);old_target=set(scenery.table_for(p,target,p.composed()))
    operations=area_layout.group(p,source,[12,13],'transfer',32,0,destination=destination)
    apply(p,operations);q=Project(p.root)
    assert not ({12,13}&set(scenery.table_for(q,source,q.composed())))
    added=set(scenery.table_for(q,target,q.composed()))-old_target
    assert len(added)==2
    apply(q,area_layout.group(q,target,sorted(added),'delete'))
    assert set(scenery.table_for(q,target,q.composed()))==old_target
    q.undo(q.doc['revision']);assert added<=set(scenery.table_for(q,target,q.composed()))
    q.undo(q.doc['revision']);assert q.doc['map_edits']==before['map_edits']
