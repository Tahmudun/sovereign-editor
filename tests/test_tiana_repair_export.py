"""Export the reported fixes through Project; preserve earlier delivery files."""
import copy
import json
from pathlib import Path
import struct

import ndspy.narc
import ndspy.rom
import pytest

from sovereign_editor.core import Project
from sovereign_editor.formats import EditorError, digest
from sovereign_editor import battle_safety as safety, character_runtime as cr, dialogue_format as fmt, event_authoring as ev, story_authoring as story
from historical import exported

ROOT = Path(__file__).resolve().parents[1]


def test_repair_export_preservation_noop_stale_undo_redo(tmp_path):
    source = Project(ROOT/'projects/tiana-events-1')
    source_doc = source.path.read_bytes()
    p = source.clone(tmp_path/'project', 'Tiana repair verification')
    op = {'kind':'interaction', 'context':{'header':72,'cell':[0,0]},
          'request':{'action':'delete','identity':'simple:54','label':'Remove the old resident below Tiana'}}
    before = p.path.read_bytes()
    assert len(p.plan_area_edit([op])['transactions']) == 1
    assert p.path.read_bytes() == before
    p.apply_area_edit(26, operations=[op], label='Tiana native retest repairs')
    assert p.doc['revision'] == 27 and p.doc['map_edits'][:-1] == source.doc['map_edits']
    p = Project(p.root)
    assert not p.simple_interactions(header=72, cell=[0,0])
    # Existing shared operations still refuse stale and changed before-values.
    before = p.path.read_bytes()
    with pytest.raises(EditorError, match='revision'): p.apply_area_edit(26, operations=[op])
    trial = copy.deepcopy(p.doc); trial['map_edits'][-1]['before']['x'] += 1
    with pytest.raises(EditorError): p._validate_state(trial)
    assert p.path.read_bytes() == before
    trainer = copy.deepcopy(story.catalog(p.composed(),'trainer')['tiana']);trainer.pop('trainer_id')
    same = {'kind':'story','context':{'header':72,'cell':[0,0]},'request':{'kind':'trainer','key':'tiana','value':trainer}}
    assert not p.apply_area_edit(27,operations=[same])['changed']
    assert p.path.read_bytes() == before
    save = ROOT/'projects/tiana-events-1/exports/tiana-r26/game.sav'
    out = tmp_path/'first'; p.export(out,27,save)
    raw = (out/'game.nds').read_bytes();rom = ndspy.rom.NintendoDSRom(raw)
    prior = ndspy.rom.NintendoDSRom(exported(ROOT/'projects/tiana-events-1/exports/tiana-r26/game.nds'))
    allowed = {'a/0/1/2':856,'a/0/2/7':555,'a/0/3/2':69}
    # Battle repairs replace whole overlay files (002 before-move, 003 stat clamp).
    overlays = {cr.overlay(p.blob, i)['file_id']: repair(cr.overlay(p.blob, i)['data'])
                for i, repair in ((137, safety.stat_stage_overlay), (142, safety.before_move_overlay))}
    changed = []
    for fid,(old,new) in enumerate(zip(prior.files,rom.files)):
        if old == new: continue
        if fid in overlays:
            assert new == overlays[fid]; changed.append(fid); continue
        name = prior.filenames.filenameOf(fid); assert name in allowed, name
        changed.append(name)
        a,b = ndspy.narc.NARC(old).files,ndspy.narc.NARC(new).files
        assert len(a)==len(b)
        assert [i for i,(x,y) in enumerate(zip(a,b)) if x!=y] == [allowed[name]]
    assert set(changed) == set(allowed) | set(overlays)
    assert prior.arm9 == rom.arm9
    def member(r,path,n): return ndspy.narc.NARC(r.getFileByName(path)).files[n]
    # The r27 Attack-down bypass is no longer installed; the native limit message returns.
    assert member(rom,safety.EFFECT_ARCHIVE,18) == safety.ORIGINAL
    oldevents = ev.records(member(prior,'a/0/3/2',69));events = ev.records(member(rom,'a/0/3/2',69))
    assert [r['id'] for r in events if r['kind']=='npc'] == [0,1,57]
    for r in oldevents:
        if r['kind']=='npc' and r['id'] in (0,1):
            assert next(v for v in events if v['kind']=='npc' and v['id']==r['id'])['raw']==r['raw']
    assert [(r['x'],r['z']) for r in events if r['kind']=='trigger'] == [(4,7)]
    scripts=member(rom,fmt.SCRIPT_ARCHIVE,856);entries=fmt.script_entries(scripts)[1]
    welcome=scripts[entries[3]:entries[4]]
    assert struct.pack('<HB2H',45,3,49,53) in welcome
    assert (out/'game.sav').read_bytes()==save.read_bytes()
    repeat=tmp_path/'repeat';p.export(repeat,27,save)
    assert (repeat/'game.nds').read_bytes()==raw
    assert (repeat/'game.sav').read_bytes()==save.read_bytes()
    p.undo(27);assert p.doc['map_edits']==source.doc['map_edits']
    undo=tmp_path/'undo';p.export(undo,28,save)
    undo_rom=ndspy.rom.NintendoDSRom((undo/'game.nds').read_bytes())
    assert member(undo_rom,'a/0/3/2',69)==member(prior,'a/0/3/2',69)
    assert p.simple_interactions(header=72,cell=[0,0])[0]['identity']=='simple:54'
    p.redo(28);redone=tmp_path/'redo';p.export(redone,29,save)
    assert (redone/'game.nds').read_bytes()==raw
    assert source.path.read_bytes()==source_doc
    report={'rom_sha256':digest(raw),'save_sha256':digest(save.read_bytes()),
            'changed_members':allowed,'arm9_exact_to_r26':True,'stock_residents_exact':True,
            'repeat_exact':True,'undo_resident_restored':True,'redo_exact':True,
            'preview_noop_readonly':True,'stale_tamper_refused':True,'original_project_exact':True}
    # The delivered r27 evidence is historical; never rewrite it from a later code state.
    (tmp_path/'export-verification.json').write_text(json.dumps(report,indent=2)+'\n')
