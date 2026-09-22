"""NPC behavior, historical repair and native geometry regression contracts."""
import copy
import json
import shutil
import struct
from pathlib import Path
import ndspy.model
import ndspy.narc
import ndspy.rom
import numpy as np
import pytest
from sovereign_editor.core import Project,atomic_json
from sovereign_editor import nitro,mapscene,surface_authoring,surface_format,world,event_authoring
from sovereign_editor.formats import EditorError,map_data

CG=dict(header=67,cell=[17,12])


@pytest.fixture(scope='module')
def workspace(tmp_path_factory):
    root=tmp_path_factory.mktemp('behavior')/'project';root.mkdir()
    for name in ('baseline.nds','project.json'):shutil.copy2(Path('projects/map-area-authoring-1')/name,root/name)
    return root,json.loads((root/'project.json').read_text())


@pytest.fixture
def p(workspace):
    root,doc=workspace;atomic_json(root/'project.json',doc)
    yield Project(root)
    atomic_json(root/'project.json',doc)


def edit(p,**values):
    identity=next(s['identity'] for s in p.simple_interactions(**CG) if s['kind']=='npc')
    return dict(kind='interaction',context=CG,request=dict(action='edit',identity=identity,**values))


def repair_operations():
    return [dict(kind='surface',context=dict(header=h,cell=c),request=dict(x=x,z=z,width=1,height=1,material=mat))
            for h,c,x,z,mat in [(61,[0,0],5,12,'la_yukad'),(33,[18,12],579,398,'grass01'),(67,[17,12],557,402,'road01')]]


def apply(p,ops):return p.apply_area_edit(p.doc['revision'],operations=ops)


def commands(raw):
    cur=0
    while cur<len(raw):
        packet=raw[cur:cur+4];cur+=4
        for code in packet:
            n=ndspy.model._COMMANDS_BY_ID[code].PARAM_COUNT
            yield code,raw[cur:cur+n*4]
            cur+=n*4
    assert cur==len(raw)


def display_lists(raw):
    _,_,model,shape,draws=surface_format.layout(raw);entries=nitro.info(model,shape)
    result={}
    for sid,_,_ in draws:
        pos=shape+struct.unpack('<I',entries[sid][1])[0];dl,length=struct.unpack_from('<2I',model,pos+8)
        result[sid]=model[pos+dl:pos+dl+length]
    return result


def test_historical_export_repair_and_whole_undo(p,tmp_path):
    prior=Path('projects/map-area-authoring-1/exports/area-r21/game.nds').read_bytes()
    p.export(tmp_path/'prior',21);assert (tmp_path/'prior/game.nds').read_bytes()==prior
    original=p.path.read_bytes()
    ops=repair_operations()+[dict(kind='map',context=CG,request={'permissions':[dict(x=556,z=400,before='0080',after='0000')]}),
        edit(p,movement=5,range_x=1,range_z=0,sprite=341)]
    plan=p.plan_area_edit(ops);assert len(plan['transactions'])==5 and p.path.read_bytes()==original
    apply(p,ops);q=Project(p.root);q.export(tmp_path/'repaired',22)
    before=ndspy.rom.NintendoDSRom(prior);after=ndspy.rom.NintendoDSRom((tmp_path/'repaired/game.nds').read_bytes())
    for fid,(a,b) in enumerate(zip(before.files,after.files)):
        if before.filenames.filenameOf(fid) not in (world.EVENT_ARCHIVE,world.MAP_ARCHIVE):assert a==b
    old=ndspy.narc.NARC(before.getFileByName(world.EVENT_ARCHIVE)).files
    new=ndspy.narc.NARC(after.getFileByName(world.EVENT_ARCHIVE)).files
    assert all(a==b for i,(a,b) in enumerate(zip(old,new)) if i!=64)
    old_actor=next(r for r in event_authoring.records(old[64]) if r['kind']=='npc' and r['id']==35)
    actor=next(r for r in event_authoring.records(new[64]) if r['kind']=='npc' and r['id']==35)
    assert (actor['sprite'],actor['movement'],actor['range_x'],actor['range_z'])==(341,5,1,0)
    expected=bytearray(old_actor['raw']);struct.pack_into('<2H',expected,2,341,5);struct.pack_into('<h',expected,20,1)
    assert actor['raw']==bytes(expected)
    assert not q.permission_cells(**CG,x=556,z=400)['cells'][0]['blocked']
    q.undo(22);q.export(tmp_path/'undo',23)
    assert (tmp_path/'undo/game.nds').read_bytes()==prior
    assert q.doc['map_edits']==json.loads(original)['map_edits']


@pytest.mark.parametrize('header,cell',[(61,[0,0]),(33,[18,12]),(67,[17,12])])
def test_native_untouched_polygons_and_lighting_are_preserved(p,header,cell):
    apply(p,repair_operations());ctx=p.context(header=header,cell=cell)
    raw=map_data(p.member_raw(ctx['map_member']))[2]
    new=surface_authoring.model(p,ctx,p.composed());tileset=mapscene.tilesets(p,ctx)[1]['map_tileset']
    _,a=nitro.decode_model(raw,tileset=tileset);_,b=nitro.decode_model(new,tileset=tileset)
    cells=p.composed()['surfaces'][ctx['map_member']]
    def key(prim,indices):
        arr=np.column_stack([prim.vertices,prim.uvs,prim.shade_commands,
                            np.where(prim.shade_commands==0x21,prim.normals,0),prim.colors])
        return tuple(tuple(v) for v in np.round(arr[indices],6))
    untouched=0;quads=0
    for pa,pb in zip(a,b):
        actual={key(pb,poly) for poly in pb.polygons}
        for poly in pa.polygons:
            points=pa.vertices[poly]
            if any(surface_area(points,x,z)>1e-5 for x,z in cells):continue
            # Vertex color is a lighting result for NORMAL; preview color is
            # material diffuse in both decodes, with no fabricated COLOR command.
            assert key(pa,poly) in actual
            untouched+=1;quads+=len(poly)==4
    assert untouched>20 and quads>5
    if header==61:
        for prim in b:
            if 'yuka' in prim.material['name']:assert np.all(prim.shade_commands==0x20)
        # All stock Lab shapes are baked-color lists; no inserted zero normal.
        assert not any(code==0x21 for dl in display_lists(new).values() for code,_ in commands(dl))


def surface_area(points,x,z):
    _,inside=surface_format.pieces(list(points),[x*16-256,z*16-256,(x+1)*16-256,(z+1)*16-256])
    return sum(abs(np.cross(t[1,:3]-t[0,:3],t[2,:3]-t[0,:3])[1])/2 for t in surface_format.tris(inside))


def test_behavior_partial_edit_duplicate_delete_and_noop(p):
    before=p.path.read_bytes();apply(p,[edit(p,movement=0,range_x=0,range_z=0,sprite=325)])
    assert p.path.read_bytes()==before
    apply(p,[edit(p,movement=5,range_x=1,range_z=0,sprite=341,facing=3)])
    apply(p,[edit(p,dialogue='A pleasant walk.')])
    actor=next(s for s in p.simple_interactions(**CG) if s['kind']=='npc')
    assert (actor['sprite'],actor['movement'],actor['range_x'],actor['facing'])==(341,5,1,3)
    before=p.path.read_bytes();apply(p,[edit(p,dialogue='A pleasant walk.')]);assert p.path.read_bytes()==before
    apply(p,[dict(kind='interaction',context=CG,request=dict(action='duplicate',identity=actor['identity'],x=553,z=403))])
    duplicate=p.simple_interactions(**CG)[-1]
    assert (duplicate['sprite'],duplicate['movement'],duplicate['range_x'],duplicate['facing'])==(341,5,1,3)
    apply(p,[dict(kind='interaction',context=CG,request=dict(action='delete',identity=duplicate['identity']))])
    assert len(p.simple_interactions(**CG))==2


@pytest.mark.parametrize('values,code',[
    ({'movement':5,'range_x':1,'range_z':0,'x':560,'z':401},'BLOCKED_TILE'),
    ({'movement':5,'range_x':1,'range_z':0,'x':558,'z':402},'EVENT_CONFLICT'),
    ({'movement':5,'range_x':1,'range_z':0,'x':563,'z':408},'EVENT_CONFLICT'),
    ({'movement':5,'range_x':1,'range_z':0,'x':544,'z':403},'OUTSIDE_MAP'),
    ({'movement':3,'range_x':0,'range_z':1},'INVALID_INPUT'),
    ({'movement':2,'range_x':1},'INVALID_INPUT'),
    ({'movement':57},'INVALID_INPUT'),
    ({'movement':5,'range_x':True},'INVALID_INPUT'),
    ({'sprite':8192},'INVALID_INPUT'),
])
def test_refusal_never_writes(p,values,code):
    before=p.path.read_bytes()
    with pytest.raises(EditorError) as exc:apply(p,[edit(p,**values)])
    assert exc.value.code==code and p.path.read_bytes()==before


def test_versioned_transactions_tampering_and_stale_guard(p):
    apply(p,repair_operations()+[edit(p,movement=5,range_x=1,range_z=0,sprite=341)])
    doc=copy.deepcopy(p.doc)
    for field in ('after','dependencies'):
        bad=copy.deepcopy(doc)
        if field=='after':bad['map_edits'][-1]['after']['movement']=0
        else:bad['map_edits'][-1]['dependencies']['appearance']['sha256']='0'*64
        atomic_json(p.path,bad)
        with pytest.raises(EditorError):Project(p.root)
    atomic_json(p.path,doc);q=Project(p.root);before=q.path.read_bytes()
    with pytest.raises(EditorError) as exc:q.apply_area_edit(21,operations=[edit(q,movement=2,range_x=0,range_z=0)])
    assert exc.value.code=='STALE_REVISION' and q.path.read_bytes()==before


def test_later_collision_change_cannot_break_movement_area(p):
    apply(p,[edit(p,movement=5,range_x=1,range_z=0)])
    before=p.path.read_bytes()
    with pytest.raises(EditorError) as exc:apply(p,[dict(kind='map',context=CG,request={'permissions':[dict(x=556,z=403,before='0000',after='0080')]})])
    assert exc.value.code=='BLOCKED_TILE' and p.path.read_bytes()==before


def test_cli_catalog_dry_run_and_internal_version_refusal(p,tmp_path,capsys):
    from sovereign_editor.cli import main
    before=p.path.read_bytes()
    main(['npc-appearances','--project',str(p.root)])
    result=json.loads(capsys.readouterr().out)['result']
    assert len(result['appearances'])==25 and result['behaviors']['5']=='Walk west / east'
    request=tmp_path/'request.json';request.write_text(json.dumps({'operations':[edit(p,movement=5,range_x=1,range_z=0)]}))
    main(['area-edit','--project',str(p.root),'--request',str(request),'--dry-run'])
    result=json.loads(capsys.readouterr().out)['result'];assert not result['empty']
    assert p.path.read_bytes()==before
    bad=edit(p);bad['request']['_version']=1
    with pytest.raises(EditorError) as exc:apply(p,[bad])
    assert exc.value.code=='INVALID_INPUT' and p.path.read_bytes()==before


def test_surface_version_cannot_be_forged(p):
    apply(p,repair_operations());doc=copy.deepcopy(p.doc)
    doc['map_edits'][-1]['writer_before']=2;atomic_json(p.path,doc)
    with pytest.raises(EditorError) as exc:Project(p.root)
    assert exc.value.code=='BEFORE_VALUE_MISMATCH'


@pytest.mark.parametrize('movement,rx,rz,x,z',[
    (0,0,0,557,403),(2,0,0,557,403),(3,1,1,554,405),
    (4,0,1,556,403),(5,1,0,557,403)])
def test_each_supported_behavior_writes_exact_native_fields(p,movement,rx,rz,x,z):
    apply(p,[edit(p,movement=movement,range_x=rx,range_z=rz,x=x,z=z,sprite=341)])
    q=Project(p.root)
    actor=event_authoring.lookup(q,64,'npc',35,q.composed())
    assert struct.unpack_from('<H',actor['raw'],4)[0]==movement
    assert struct.unpack_from('<2h',actor['raw'],20)==(rx,rz)
    assert (actor['x'],actor['z'],actor['sprite'])==(x,z,341)
