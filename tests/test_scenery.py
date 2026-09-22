"""Structural authoring: identity, composition and independent ROM/NARC readback."""
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

from sovereign_editor.core import Project, atomic_json
from sovereign_editor.formats import EditorError, resource, map_sections
from sovereign_editor import world

CG = dict(header=67, cell=[17, 12])
NB = dict(header=60, cell=[21, 12])
ROUTE = dict(header=33, cell=[18, 12])


@pytest.fixture(scope='module')
def workspace(tmp_path_factory):
    source = Path('projects/map-adjacent-1')
    if not (source/'baseline.nds').is_file():
        pytest.skip('local accepted r8 project required')
    root = tmp_path_factory.mktemp('scenery')/'project'
    root.mkdir()
    for file in ('project.json', 'baseline.nds'):
        shutil.copy2(source/file, root/file)
    p = Project(root)
    return root, copy.deepcopy(p.doc)


@pytest.fixture
def project(workspace):
    root, original = workspace
    atomic_json(root/'project.json', original)
    yield Project(root)
    atomic_json(root/'project.json', original)


def apply(p, action='duplicate', slot=12, context=CG, **kw):
    if action not in ('delete',):
        kw.setdefault('x', 553.5); kw.setdefault('z', 398.5)
    return p.apply_scenery_edit(p.doc['revision'], operation=action, slot=slot, **context, **kw)


def decode(blob, member):
    # Independent libraries resolve the ROM and NARC allocations. struct computes
    # section offsets, without the product's reader/writer.
    rom = ndspy.rom.NintendoDSRom(blob)
    narc = ndspy.narc.NARC(rom.getFileByName(world.MAP_ARCHIVE))
    raw = narc.files[member]
    perm, builds, model, bdhc = struct.unpack_from('<4I', raw)
    extra = struct.unpack_from('<H', raw, 18)[0]
    start = 20 + extra + perm
    assert len(raw) == start + builds + model + bdhc
    return rom, narc, raw, [raw[i:i+48] for i in range(start, start+builds, 48)]


def test_add_move_delete_preserves_identity_and_reopen(project):
    before = project.map_view(**CG, include_grid=False)['placements']
    result = apply(project)
    created = result['preview']['objects'][0]
    apply(project, 'delete', slot=11)
    project.apply_map_edit(project.doc['revision'], **CG, placement={'slot':created['slot'], 'x':554.5, 'z':398.5})
    project.apply_map_edit(project.doc['revision'], **CG, placement={'slot':12, 'x':552.5, 'z':398.5})
    reopened = Project(project.root)
    objects = reopened.map_view(**CG, include_grid=False)['placements']
    clone = next(o for o in objects if o['object_id'] == created['id'])
    assert clone['position']['x'] == 554.5
    assert clone['slot'] == created['slot'] and clone['export_slot'] != clone['slot']
    assert next(o for o in objects if o['slot']==12)['position']['x']==552.5
    assert len(objects)==len(before)
    with pytest.raises(EditorError, match='removed|transferred'):
        project.apply_map_edit(project.doc['revision'], **CG, placement={'slot':11, 'x':550.5})


def test_grown_export_preserves_all_files_and_member_sections(project, tmp_path):
    original_manifest=(project.root/'project.json').read_bytes()
    result=apply(project)
    slot=result['preview']['objects'][0]['slot']
    result=project.export(tmp_path/'grown', project.doc['revision'])
    assert result['allocation']['relocated'] is True
    output=(tmp_path/'grown/game.nds').read_bytes()
    rom,narc,raw,records=decode(output,5)
    base_rom,base_narc,base_raw,baseline_records=decode(project.blob,5)
    assert len(records)==len(baseline_records)+1
    assert records[-1][16:]==baseline_records[12][16:]
    assert records[-1][:4]==baseline_records[12][:4]
    assert struct.unpack_from('<3i',records[-1],4)==(int((553.5-560)*65536),65536,int((398.5-400)*65536))
    old_start=20+struct.unpack_from('<H',base_raw,18)[0]+2048+len(baseline_records)*48
    new_start=20+struct.unpack_from('<H',raw,18)[0]+2048+len(records)*48
    assert raw[new_start:]==base_raw[old_start:]
    for i,(a,b) in enumerate(zip(base_narc.files,narc.files)):
        if i not in (0,5): assert a==b
    # All files except map/event archives are exact; verify event archive against accepted r8.
    accepted=ndspy.rom.NintendoDSRom(Path('projects/map-adjacent-1/exports/adjacent-and-sign-r8/game.nds').read_bytes())
    for i,(a,b) in enumerate(zip(accepted.files,rom.files)):
        if i!=rom.filenames.idOf(world.MAP_ARCHIVE): assert a==b
    assert len(output)==struct.unpack_from('<I',output,0x80)[0]
    from ndspy._common import crc16
    assert struct.unpack_from('<H',output,0x15e)[0]==crc16(output[:0x15e])
    project.undo(project.doc['revision'])
    restored=project.export(tmp_path/'undo',project.doc['revision'])
    assert restored['candidate_sha256']=='ad3735d0c770395fd30e3c7081e62bcc8292a41a3419348f02eb4a5af22f17b6'


def test_delete_before_sign_keeps_binding(project,tmp_path):
    apply(project,'delete',slot=1,context=NB)
    project.apply_map_edit(project.doc['revision'], **NB, placement={'slot':13,'x':683.5,'z':400.5}, align_sign=True)
    report=project.export(tmp_path/'sign',project.doc['revision'])
    _,_,_,records=decode((tmp_path/'sign/game.nds').read_bytes(),0)
    assert struct.unpack_from('<I',records[12])[0]==29
    assert struct.unpack_from('<i',records[12],4)[0]==int((683.5-688)*65536)
    assert project.map_sign(**NB)['position']==[683,400]
    assert project.map_view(**NB,include_grid=False)['placements'][12]['slot']==13
    with pytest.raises(EditorError) as exc:apply(project,'delete',slot=13,context=NB)
    assert exc.value.code=='BOUND_OBJECT'


def test_transfer_two_members_and_undo(project):
    before=json.dumps(project.doc,sort_keys=True)
    result=apply(project,'transfer',x=578.5,z=398.5,destination=ROUTE)
    changes=result['preview']['objects']
    assert changes[0]['id']==changes[1]['id']=='baseline:5:12'
    assert not any(o['slot']==12 for o in project.map_view(**CG,include_grid=False)['placements'])
    target=next(o for o in project.map_view(**ROUTE,include_grid=False)['placements'] if o['object_id']=='baseline:5:12')
    assert target['position']['x']==578.5
    project.undo(project.doc['revision'])
    assert any(o['slot']==12 for o in project.map_view(**CG,include_grid=False)['placements'])
    assert len(project.map_view(**ROUTE,include_grid=False)['placements'])==1


def test_transfer_collision_and_atomic_refusal(project):
    # Accepted west planter is at x551. Its three explicit blockers move with it.
    cells=[{'x':551,'z':z} for z in (397,398,399)]
    result=apply(project,'transfer',x=578.5,z=398.5,destination=ROUTE,move_collision=cells)
    assert len(result['preview']['permission_cells'])==6
    assert all(not c['blocked'] for c in project.permission_cells(**CG,x=551,z=397,height=3)['cells'])
    assert all(c['blocked'] for c in project.permission_cells(**ROUTE,x=578,z=397,height=3)['cells'])
    project.undo(project.doc['revision'])
    old=(project.root/'project.json').read_bytes()
    with pytest.raises(EditorError):
        apply(project,'transfer',x=608.0,z=398.5,destination=ROUTE,move_collision=cells)
    assert (project.root/'project.json').read_bytes()==old


@pytest.mark.parametrize('edit_request,code',[
    ({'destination':CG,'x':552.5},'SHARED_RESOURCE'),
    ({'destination':{'header':61,'cell':[0,0]},'x':10.5,'z':10.5},'NOT_ADJACENT'),
    ({'destination':{'header':33,'cell':[20,12]},'x':644.5},'NOT_ADJACENT'),
])
def test_transfer_refusals(project,edit_request,code):
    before=(project.root/'project.json').read_bytes()
    with pytest.raises(EditorError) as exc:apply(project,'transfer',**edit_request)
    assert exc.value.code==code
    assert (project.root/'project.json').read_bytes()==before


def test_palette_indoor_and_bound_templates(project):
    palette=project.map_palette(**CG)
    assert any(t['model_id']==52 and t['status']=='ok' for t in palette['templates'])
    nb=project.map_palette(**NB)
    assert next(t for t in nb['templates'] if t['slot']==13)['code']=='BOUND_OBJECT'
    indoor=project.map_palette(header=61,cell=[0,0])
    template=next(t for t in indoor['templates'] if t['status']=='ok')
    apply(project,'add',slot=template['slot'],context={'header':61,'cell':[0,0]},x=12.5,z=12.5)
    assert len(project.map_view(header=61,cell=[0,0],include_grid=False)['placements'])==16


def test_duplicate_collision_copies_and_preserves_source(project):
    result=apply(project,move_collision=[{'x':551,'z':z} for z in (397,398,399)])
    assert len(result['preview']['permission_cells'])==3
    assert all(c['blocked'] for c in project.permission_cells(**CG,x=551,z=397,height=3)['cells'])
    assert all(c['blocked'] for c in project.permission_cells(**CG,x=553,z=397,height=3)['cells'])


def test_stale_and_forged_transaction_refuse(project):
    rev=project.doc['revision'];apply(project)
    with pytest.raises(EditorError) as exc:
        project.apply_scenery_edit(rev,operation='delete',slot=12,**CG)
    assert exc.value.code=='STALE_REVISION'
    tampered=copy.deepcopy(project.doc)
    tampered['map_edits'][-1]['objects'][0]['after']='00'*48
    with pytest.raises(EditorError) as exc:project._validate_state(tampered)
    assert exc.value.code=='BEFORE_VALUE_MISMATCH'
    tampered=copy.deepcopy(project.doc);tampered['map_edits'][-1]['index']+=1
    with pytest.raises(EditorError):project._validate_state(tampered)


def test_net_zero_structural_export_is_accepted_r8(project,tmp_path):
    created=apply(project)['preview']['objects'][0]['slot']
    apply(project,'delete',slot=created)
    a=project.export(tmp_path/'a',project.doc['revision'])
    b=project.export(tmp_path/'b',project.doc['revision'])
    assert a['candidate_sha256']==b['candidate_sha256']=='ad3735d0c770395fd30e3c7081e62bcc8292a41a3419348f02eb4a5af22f17b6'
    assert not a['allocation']['relocated']


def test_cli_uses_same_plan_and_revision(project):
    cmd=[sys.executable,'-m','sovereign_editor.cli','map-scenery','--project',str(project.root),
         '--header','67','--cell','17,12','--action','duplicate','--slot','12','--to-x','553.5','--to-z','398.5']
    dry=json.loads(subprocess.check_output(cmd+['--dry-run'],text=True))['result']
    assert dry['preview']==project.plan_scenery_edit(operation='duplicate',slot=12,x=553.5,z=398.5,**CG)['preview']
    result=json.loads(subprocess.check_output(cmd+['--revision',str(project.doc['revision'])],text=True))
    assert result['result']['changed']
    assert len(Project(project.root).map_view(**CG,include_grid=False)['placements'])==16


def test_empty_structural_state_exports_exact_baseline(project,tmp_path):
    doc=copy.deepcopy(project.doc)
    doc.update(revision=0,positions={},placement_moves={},map_edits=[],history=[])
    doc['unknown_user_metadata']={'preserve':['all',17]}
    atomic_json(project.path,doc)
    p=Project(project.root)
    created=apply(p)['preview']['objects'][0]['slot']
    apply(p,'delete',slot=created)
    report=p.export(tmp_path/'baseline',p.doc['revision'])
    assert report['candidate_sha256']==doc['baseline']['sha256']
    assert report['changed_byte_count']==0 and not report['allocation']['changed']
    assert Project(p.root).doc['unknown_user_metadata']=={'preserve':['all',17]}
