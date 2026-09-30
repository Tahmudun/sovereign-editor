"""Successor project (r28): no-op, stale refusal, repeat export, Undo/Redo and r27 isolation.

Runs on a clone; the delivered projects/scyther-quest-1 and r27 inputs are never written.
Software checks only, not melonDS acceptance.
"""
import copy
from pathlib import Path

import ndspy.narc
import ndspy.rom
import pytest

from sovereign_editor.core import Project
from sovereign_editor.formats import EditorError
from sovereign_editor import dialogue_format as fmt, story_authoring as story, world
from tests.test_scene_entry_follower import movement_targets, native_commands

ROOT = Path(__file__).resolve().parents[1]
SUCCESSOR = ROOT / 'projects/scyther-quest-1'
R27 = ROOT / 'projects/tiana-fixes-1'
SAVE = R27 / 'exports/tiana-r27/game.sav'
pytestmark = pytest.mark.skipif(not (SUCCESSOR / 'project.json').exists(), reason='successor project absent')


def members(raw, path):
    return ndspy.narc.NARC(ndspy.rom.NintendoDSRom(raw).getFileByName(path)).files


def changed_files(a, b):
    a, b = ndspy.rom.NintendoDSRom(a), ndspy.rom.NintendoDSRom(b)
    assert (a.arm9, a.arm7, a.arm9OverlayTable, len(a.files)) == (b.arm9, b.arm7, b.arm9OverlayTable, len(b.files))
    return {a.filenames.filenameOf(i) or i for i, (x, y) in enumerate(zip(a.files, b.files)) if x != y}


def test_successor_noop_stale_repeat_undo_redo(tmp_path):
    delivered = (SUCCESSOR / 'project.json').read_bytes(); r27_doc = (R27 / 'project.json').read_bytes()
    p = Project(SUCCESSOR).clone(tmp_path / 'project', 'Scyther delivery verification')
    assert p.doc['revision'] == 28
    assert p.doc['map_edits'][:len(Project(R27).doc['map_edits'])] == Project(R27).doc['map_edits']
    before = p.path.read_bytes()
    practice = copy.deepcopy(story.catalog(p.composed(), 'sequence')['tiana_practice'])
    for k in ('context', 'event_member', 'script_member', 'text_member', 'y', 'npc_id', 'hide_flag'):
        practice.pop(k, None)
    upgrade = {'kind': 'story', 'context': {'header': 72, 'cell': [0, 0]},
               'request': {'kind': 'sequence', 'key': 'tiana_practice', 'value': practice}}
    # Re-putting an r28 (v1) scene brings it under the repaired v2 rules once, unchanged.
    (t,) = p.plan_area_edit([upgrade])['transactions']
    assert t['schema'] == story.SCENE_SCHEMA and t['before'] == t['after']
    assert p.area_preview_project(p.plan_area_edit([upgrade])).plan_area_edit([upgrade])['empty']
    quest = story.catalog(p.composed(), 'state')['scyther_quest']
    same = {'kind': 'story', 'context': {'header': 72, 'cell': [0, 0]},
            'request': {'kind': 'state', 'key': 'scyther_quest', 'value': {'name': quest['name']}}}
    assert not p.apply_area_edit(28, operations=[same])['changed'] and p.path.read_bytes() == before
    with pytest.raises(EditorError, match='revision'):
        p.apply_area_edit(27, operations=[same])
    first = tmp_path / 'first'; p.export(first, 28, SAVE)
    raw = (first / 'game.nds').read_bytes()
    delivered_rom = SUCCESSOR / 'exports/scyther-r28/game.nds'
    if delivered_rom.exists():
        # r28 froze at the beach: its Cherrygrove/Route 29 movement data sat at odd
        # offsets. The repaired compiler changes only those scene scripts; overlays
        # 137/142 (stat repairs) and every other file stay byte-identical.
        old = delivered_rom.read_bytes()
        assert changed_files(raw, old) == {fmt.SCRIPT_ARCHIVE}
        new_scripts, old_scripts = members(raw, fmt.SCRIPT_ARCHIVE), members(old, fmt.SCRIPT_ARCHIVE)
        assert {i for i, (x, y) in enumerate(zip(new_scripts, old_scripts)) if x != y} == {225, 850, 856}
        stock = members(p.blob, fmt.SCRIPT_ARCHIVE)
        for member in (225, 850, 856):
            start = fmt.script_entries(new_scripts[member])[1][len(fmt.script_entries(stock[member])[1])]
            targets = movement_targets(new_scripts[member], start)
            assert targets and all(t % 4 == 0 and native_commands(new_scripts[member], t) for _, t in targets)
    repeat = tmp_path / 'repeat'; p.export(repeat, 28, SAVE)
    assert (repeat / 'game.nds').read_bytes() == raw and (repeat / 'game.sav').read_bytes() == SAVE.read_bytes()
    p.undo(28)
    undone = tmp_path / 'undo'; p.export(undone, 29, SAVE)
    r27_export = tmp_path / 'r27'; Project(R27).clone(tmp_path / 'r27-project', 'r27 now').export(r27_export, 27, SAVE)
    for path in (fmt.SCRIPT_ARCHIVE, fmt.TEXT_ARCHIVE, world.EVENT_ARCHIVE):
        assert members((undone / 'game.nds').read_bytes(), path) == members((r27_export / 'game.nds').read_bytes(), path)
    p.redo(29)
    redone = tmp_path / 'redo'; p.export(redone, 30, SAVE)
    assert (redone / 'game.nds').read_bytes() == raw
    assert (SUCCESSOR / 'project.json').read_bytes() == delivered and (R27 / 'project.json').read_bytes() == r27_doc
