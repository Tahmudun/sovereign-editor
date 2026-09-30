import copy
import json
import struct
from pathlib import Path
import pytest
import ndspy.rom
import ndspy.narc
from sovereign_editor.core import Project
from sovereign_editor.formats import EditorError,resource,digest
from sovereign_editor import story_authoring as story,character_runtime as cr,dialogue_format as fmt
from tools.tiana_encounter import operations,CONTEXT
from historical import exported

ROOT=Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def p(tmp_path_factory):
    p=Project(ROOT/'projects/map-workflow-1').clone(tmp_path_factory.mktemp('story')/'project','Tiana integration verification')
    ops=operations(json.loads((ROOT/'tests/fixtures/tiana.character.json').read_text()))
    original=p.path.read_bytes();plan=p.plan_area_edit(ops)
    assert p.path.read_bytes()==original and len(plan['transactions'])==8
    p.apply_area_edit(p.doc['revision'],operations=ops)
    assert p.doc['revision']==24 and len(p.doc['history'])==22
    (ROOT/'evidence/tiana-events-1/verified-project-location.txt').write_text(str(p.root)+'\n')
    return p


def definition(p,kind,key):
    v=copy.deepcopy(story.catalog(p.composed(),kind)[key])
    if kind=='sequence':
        for k in ('context','event_member','script_member','text_member','y','npc_id'):v.pop(k)
    elif kind=='trainer':v.pop('trainer_id')
    return {'kind':'story','context':CONTEXT,'request':{'kind':kind,'key':key,'value':v}}


def test_reopen_noop_stale_and_tamper(p):
    reopened=Project(p.root);assert reopened.story_library()==p.story_library()
    op=definition(p,'sequence','tiana_practice');before=p.path.read_bytes()
    result=p.apply_area_edit(24,operations=[op]);assert not result['changed'] and p.path.read_bytes()==before
    with pytest.raises(EditorError,match='revision'):p.apply_area_edit(23,operations=[op])
    t=copy.deepcopy(p.doc['map_edits'][-1]);t['after']['x']+=1
    trial=copy.deepcopy(p.doc);trial['map_edits'][-1]=t
    with pytest.raises(EditorError):p._validate_state(trial)
    assert p.path.read_bytes()==before


def test_export_preservation_script_allocations_and_undo_redo(p,tmp_path):
    base=Project(ROOT/'projects/map-workflow-1');save=ROOT/'projects/map-workflow-1/exports/workflow-r23/game.sav'
    out=tmp_path/'first';p.export(out,p.doc['revision'],save);raw=(out/'game.nds').read_bytes();rom=ndspy.rom.NintendoDSRom(raw)
    prior=ndspy.rom.NintendoDSRom(exported(ROOT/'projects/map-workflow-1/exports/workflow-r23/game.nds'))
    def narc(r,path):return ndspy.narc.NARC(r.getFileByName(path)).files
    for path,extra in ((cr.OVERWORLD_ARCHIVE,1),(cr.FRONT_ARCHIVE,5),(cr.BACK_ARCHIVE,5),(story.TRAINER_ARCHIVE,3),(story.PARTY_ARCHIVE,3)):
        a,b=narc(prior,path),narc(rom,path);assert b[:len(a)]==a and len(b)==len(a)+extra
    for path in ('a/0/6/5','a/0/4/1'):
        assert rom.getFileByName(path)==prior.getFileByName(path)
    # Stock messages retain their encrypted contents, including unrelated banks.
    a,b=narc(prior,fmt.TEXT_ARCHIVE),narc(rom,fmt.TEXT_ARCHIVE)
    for i,(old,new) in enumerate(zip(a,b)):
        if i in (555,729,730,731):
            oldentries=fmt.text_entries(old)[1];newentries=fmt.text_entries(new)[1]
            assert [e[2] for e in newentries[:len(oldentries)]]==[e[2] for e in oldentries]
        else:assert old==new
    assert [len(narc(rom,story.PARTY_ARCHIVE)[i]) for i in range(738,741)]==[8,8,8]
    assert [struct.unpack_from('<H',narc(rom,story.TRAINER_ARCHIVE)[i],1)[0] for i in range(738,741)]==[129,2,3]
    assert len(narc(rom,story.TRAINER_OFFSETS)[0])==741*2
    assert (out/'game.sav').read_bytes()==save.read_bytes()
    plan=story.runtime(p,p.composed())
    for change in plan['patches']:
        at=change['rom_offset'];after=bytes.fromhex(change['after']);assert raw[at:at+len(after)]==after
    for file_id,payload in plan['files'].items():assert rom.files[file_id]==payload
    repeated=tmp_path/'repeat';p.export(repeated,p.doc['revision'],save)
    assert (repeated/'game.nds').read_bytes()==raw and (repeated/'game.sav').read_bytes()==save.read_bytes()
    p.undo(24);assert p.doc['map_edits']==base.doc['map_edits'] and not p.story_library()['characters']
    undone=tmp_path/'undo';p.export(undone,25,save)
    assert digest((undone/'game.nds').read_bytes())=='f97b1576a6c09096904c912a56e14b81aee3258fdd2315c63304b4cf53b7c434'
    p.redo(25);assert p.story_library()['characters']['tiana']['sprite']==7000
    redone=tmp_path/'redo';p.export(redone,26,save);assert (redone/'game.nds').read_bytes()==raw
    report={'candidate_sha256':digest(raw),'revision':26,'project':str(p.root),'checks':['preview read-only','reopen','no-op','stale refusal','tamper refusal','stock graphics retained','stock text retained','trainer resources','runtime patches','played save exact','repeat export','undo r23 identity','redo candidate identity']}
    (ROOT/'evidence/tiana-events-1/project-verification.json').write_text(json.dumps(report,indent=2)+'\n')


@pytest.mark.parametrize('issue',['blocked','overlap','bad_branch','missing_character','wrong_context'])
def test_invalid_edits_are_read_only(p,issue):
    op=definition(p,'sequence','tiana_practice');v=op['request']['value'];before=p.path.read_bytes()
    if issue=='blocked':v['x']=10
    elif issue=='overlap':v['x']=6
    elif issue=='bad_branch':v['nodes'][0]['yes']='unknown'
    elif issue=='missing_character':v['character']='unknown'
    else:op['context']={'header':71,'cell':[0,0]}
    with pytest.raises(EditorError):p.plan_area_edit([op])
    assert p.path.read_bytes()==before
