import copy
import json
import struct
from pathlib import Path

import ndspy.narc
import ndspy.rom
import pytest

from sovereign_editor import gameplay as g
from sovereign_editor.core import Project, atomic_json
from sovereign_editor.formats import EditorError, digest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def workspace(tmp_path_factory):
    p = Project.create(ROOT/'projects/scyther-orchestration-1/baseline.nds', tmp_path_factory.mktemp('gameplay')/'project')
    return p.root, copy.deepcopy(p.doc)


@pytest.fixture
def p(workspace):
    root, doc = workspace; atomic_json(root/'project.json', copy.deepcopy(doc))
    return Project(root)


def request(p):
    d = p.gameplay_data(); t = next(t for t in d['trainers'] if t['id'] == 47)
    return {'operations':[
        {'kind':'trainer','id':47,'before_sha256':t['before_sha256'],'party':[
            {'species':16,'level':4,'held_item':0,'moves':[16,33,28,0]},
            {'species':19,'level':5,'held_item':155,'moves':[98,33,39,0]}]},
        {'kind':'encounters','before_sha256':d['encounters']['before_sha256'],'edits':[
            {'method':'grass','field':'rate','value':20},
            {'method':'grass','field':'level','slot':0,'value':4},
            {'method':'grass','field':'species','time':'day','slot':0,'value':16}]}]}


def test_profile_catalog_and_real_route(p):
    d = p.gameplay_data(); assert [t['id'] for t in d['trainers']] == [249,8,47]
    assert d['trainers'][1]['editable'] is False and '2696' in d['trainers'][1]['reason']
    assert d['encounters']['methods']['grass']['levels'] == [100]*12
    assert d['encounters']['methods']['grass']['day'] == [131]*12
    assert p.gameplay_catalog('items',search='Oran')['entries'] == [g.entry(p,'items',155)]
    assert g.entry(p,'moves',16)['supported'] and g.entry(p,'moves',28)['supported']
    assert not g.entry(p,'moves',928)['supported'] and not g.entry(p,'items',2696)['supported']
    # v2 discovery: other areas are inspectable; absent resources are explained, invalid headers refused.
    assert p.gameplay_data(33)['trainers'] == [] and p.gameplay_data(134)['encounters'] is None
    with pytest.raises(EditorError): p.gameplay_data(100000)


@pytest.mark.parametrize('mode',range(4))
def test_four_strides_preserve_opaque_fields_and_noop(p,mode):
    header = bytearray(g.base(p,g.TRAINERS,47)); header[0]=mode
    parts=[]
    for species,level,override in [(16,2,0),(19,4,32)]:
        parts.append(struct.pack('<BBHH',73,override,level,species)
                     +(struct.pack('<H',155) if mode&2 else b'')
                     +(struct.pack('<4H',33,39,0,0) if mode&1 else b'')+struct.pack('<H',7))
    raw=b''.join(parts); rows=g.decode_team(header,raw)
    assert g.encode_team(p,header,raw,g.team_value(rows)) == (bytes(header),raw)
    values=g.team_value(rows); values[0]['level']=5
    h,r=g.encode_team(p,header,raw,values)
    assert h == bytes(header) and r[:2] == raw[:2] and r[4:] == raw[4:]
    assert [v['capsule'] for v in g.decode_team(h,r)] == [7,7]


def test_atomic_noop_stale_tamper_reopen_undo_redo(p):
    req=request(p); before=p.path.read_bytes(); plan=p.plan_gameplay_edit(**req)
    assert p.path.read_bytes()==before and len(plan['transaction']['changes'])==3
    bad=copy.deepcopy(req); bad['operations'][1]['edits'][0]['value']=101
    with pytest.raises(EditorError):p.apply_gameplay_edit(0,**bad)
    assert p.path.read_bytes()==before
    p.apply_gameplay_edit(0,**req); assert p.doc['revision']==1
    after=Project(p.root); assert after.gameplay_data()==p.gameplay_data()
    fresh=request(p); same=p.path.read_bytes()
    assert not p.apply_gameplay_edit(1,**fresh)['changed'] and p.path.read_bytes()==same
    with pytest.raises(EditorError,match='revision'):p.apply_gameplay_edit(0,**fresh)
    with pytest.raises(EditorError,match='before-value'):p.plan_gameplay_edit(**req)
    for field in ('dependencies','changes','index'):
        doc=copy.deepcopy(p.doc)
        if field=='dependencies':doc['map_edits'][-1][field]['event']='bad'
        elif field=='changes':doc['map_edits'][-1][field][0]['after']='00'
        else:doc['map_edits'][-1]['index']=100
        with pytest.raises(EditorError):p._validate_state(doc)
    p.undo(1);assert p.doc['map_edits']==[]
    p.redo(2);assert p.gameplay_data()['encounters']['methods']==after.gameplay_data()['encounters']['methods']


@pytest.mark.parametrize('issue',['species','bool','level','item','move','duplicate','empty','mixed','form','size','bits','zero','wrongtrainer','duplicate_target','before'])
def test_team_refusals(p,issue):
    req=request(p); op=req['operations'][0]; m=op['party'][0]; before=p.path.read_bytes()
    if issue=='species':m['species']=494
    if issue=='bool':m['species']=True
    if issue=='level':m['level']=101
    if issue=='item':m['held_item']=2696
    if issue=='move':m['moves'][0]=928
    if issue=='duplicate':m['moves']=[33,33,0,0]
    if issue=='empty':m['moves']=[0]*4
    if issue=='mixed':m['moves']=None
    if issue=='wrongtrainer':op['id']=1
    if issue=='duplicate_target':req['operations'].append(copy.deepcopy(op))
    if issue=='before':op['before_sha256']='bad'
    if issue in ('form','size','bits','zero'):
        h=bytearray(g.base(p,g.TRAINERS,47));raw=bytearray(g.base(p,g.PARTIES,47))
        if issue=='form':struct.pack_into('<H',raw,4,16|(1<<11))
        if issue=='size':raw.pop()
        if issue=='bits':h[0]=4
        if issue=='zero':h[3]=0
        with pytest.raises(EditorError):g.encode_team(p,h,raw,op['party'])
    else:
        with pytest.raises(EditorError):p.plan_gameplay_edit(**req)
    assert p.path.read_bytes()==before


def test_all_encounter_sections_and_exact_preservation(p):
    old=g.base(p,g.WILD,3); edits=[]; touched=set()
    for i,t in enumerate(g.TIMES):
        edits.append({'method':'grass','field':'species','time':t,'slot':11,'value':161})
        touched.update([20+i*24+22,20+i*24+23])
    edits.append({'method':'grass','field':'level','slot':11,'value':5});touched.add(19)
    for method,(offset,count,rate) in g.METHODS.items():
        # Disabled rock table has empty species: leave rate zero, edit one slot.
        edits.extend([{'method':method,'field':'levels','slot':0,'value':[4,8]},
                      {'method':method,'field':'species','slot':0,'value':129}])
        touched.update(range(offset,offset+4))
    new=g.encode_wild(p,old,edits)
    assert len(new)==196 and all(a==b for i,(a,b) in enumerate(zip(old,new)) if i not in touched)
    assert new[6:8]==old[6:8] and new[92:100]==old[92:100] and new[188:]==old[188:]
    for t in g.TIMES:assert g.decode_wild(new)['grass'][t][-1]==161


@pytest.mark.parametrize('edit',[
    {'method':'grass','field':'level','slot':0,'time':'day','value':3},
    {'method':'grass','field':'species','slot':12,'time':'day','value':16},
    {'method':'grass','field':'rate','value':101},
    {'method':'surf','field':'levels','slot':0,'value':[8,4]},
    {'method':'surf','field':'levels','slot':0,'value':[0,4]},
    {'method':'headbutt','field':'rate','value':5},
    {'method':'rock_smash','field':'rate','value':20},
])
def test_encounter_refusals(p,edit):
    with pytest.raises(EditorError):g.encode_wild(p,g.base(p,g.WILD,3),[edit])


def test_duplicate_field_and_final_binding_conflict(p):
    req=request(p); req['operations'][1]['edits'] *= 2
    with pytest.raises(EditorError,match='twice'):p.plan_gameplay_edit(**req)
    p.apply_gameplay_edit(0,**request(p))
    # Final validation pins the bound header resources (whole-file digests are checked at replay).
    state=copy.deepcopy(p.composed());state['gameplay_checks'][0]['headers']['34']='stale'
    with pytest.raises(EditorError,match='changed'):g.validate(p,state)


@pytest.mark.parametrize('saved',['stale','00'*23,'00'*25,None,34,b'\x00'*24])
def test_malformed_bound_header_refuses_through_editor_error(p,saved):
    p.apply_gameplay_edit(0,**request(p))
    state=copy.deepcopy(p.composed());state['gameplay_checks'][0]['headers']['34']=saved
    with pytest.raises(EditorError,match='changed') as info:g.validate(p,state)
    assert info.value.code=='CONTEXT_MISMATCH'


def test_identity_only_header_change_keeps_binding_but_rebinding_refuses(p):
    from sovereign_editor import world
    p.apply_gameplay_edit(0,**request(p))
    state=copy.deepcopy(p.composed());saved=state['gameplay_checks'][0]['headers']['34']
    raw=bytearray.fromhex(saved)
    # Location/music/weather edits keep the gameplay resources bound.
    raw[18]=(raw[18]+1)&0xFF;raw[12]^=1
    state['gameplay_checks'][0]['headers']['34']=raw.hex();g.validate(p,state)
    # A different encounter binding refuses.
    raw=bytearray.fromhex(saved);raw[0]^=1
    state['gameplay_checks'][0]['headers']['34']=raw.hex()
    with pytest.raises(EditorError,match='changed'):g.validate(p,state)
    assert world.decode_header(bytes.fromhex(saved),34,'',None)['wild_pokemon']==p.header(34)['wild_pokemon']


def test_independent_export_all_files_and_repeat(p,tmp_path):
    p.apply_gameplay_edit(0,**request(p));p.export(tmp_path/'first',1)
    raw=(tmp_path/'first/game.nds').read_bytes();rom=ndspy.rom.NintendoDSRom(raw);base=ndspy.rom.NintendoDSRom(p.blob)
    changed=[]
    for path, expected in [(g.TRAINERS,[47]),(g.PARTIES,[47]),(g.WILD,[3])]:
        i=base.filenames.idOf(path);changed.append(i)
        a=ndspy.narc.NARC(base.files[i]).files;b=ndspy.narc.NARC(rom.files[i]).files
        assert len(a)==len(b) and [j for j,(x,y) in enumerate(zip(a,b)) if x!=y]==expected
    assert [i for i,(a,b) in enumerate(zip(base.files,rom.files)) if a!=b]==sorted(changed)
    assert rom.arm9==base.arm9 and rom.arm7==base.arm7 and rom.arm9OverlayTable==base.arm9OverlayTable
    h=ndspy.narc.NARC(rom.getFileByName(g.TRAINERS)).files[47];party=ndspy.narc.NARC(rom.getFileByName(g.PARTIES)).files[47]
    assert h[0]==3 and h[3]==2 and len(party)==36
    assert struct.unpack('<BBHHH4HH',party[18:])==(0,32,5,19,155,98,33,39,0,0)
    p.export(tmp_path/'repeat',1);assert (tmp_path/'repeat/game.nds').read_bytes()==raw
    p.undo(1);p.export(tmp_path/'undo',2);assert (tmp_path/'undo/game.nds').read_bytes()==p.blob
    p.redo(2);p.export(tmp_path/'redo',3);assert (tmp_path/'redo/game.nds').read_bytes()==raw


def test_cli_uses_project(p,tmp_path,capsys):
    from sovereign_editor.cli import main
    q=tmp_path/'request.json';q.write_text(json.dumps(request(p)))
    args=['gameplay-edit','--project',str(p.root),'--request',str(q)]
    before=p.path.read_bytes();assert main(args+['--dry-run'])==0;res=json.loads(capsys.readouterr().out)
    assert res['result']['preview']==p.plan_gameplay_edit(**json.loads(q.read_text()))['preview'] and p.path.read_bytes()==before
    assert main(args+['--revision','0'])==0;capsys.readouterr()
    assert Project(p.root).gameplay_data()['revision']==1


def test_area_batch_keeps_event_and_gameplay_atomic(p):
    ctx={'header':34,'cell':[17,10]}
    ops=[{'kind':'event','context':ctx,'request':{'kind':'npc','event_id':5,'values':{'x':549,'z':348}}},
         {'kind':'gameplay','context':ctx,'request':request(p)}]
    before=p.path.read_bytes();bad=copy.deepcopy(ops);bad[1]['request']['operations'][0]['party'][0]['level']=101
    with pytest.raises(EditorError):p.apply_area_edit(0,operations=bad)
    assert p.path.read_bytes()==before
    p.apply_area_edit(0,operations=ops);assert len(p.doc['history'])==1 and len(p.doc['map_edits'])==2
    d=Project(p.root).gameplay_data();assert next(t for t in d['trainers'] if t['id']==47)['position']==[549,348]
    p.undo(1);assert p.doc['map_edits']==[]
    p.redo(2);assert len(p.doc['map_edits'])==2
