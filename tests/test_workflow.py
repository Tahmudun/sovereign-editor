"""Direct workspace transactions, real-resource isolation and durable history."""
import copy
import json
import shutil
import struct
from pathlib import Path
import ndspy.narc
import ndspy.rom
import pytest
from sovereign_editor.core import Project, atomic_json
from sovereign_editor.formats import EditorError
from sovereign_editor import world, workflow
from sovereign_editor.workflow_session import WorkflowSession
from historical import exported as historical_rom

ROOM = {'header': 72, 'cell': [0, 0]}
CG = {'header': 67, 'cell': [17, 12]}


def action(kind, request, context=ROOM):
    return {'kind': kind, 'context': context, 'request': request}


@pytest.fixture(scope='module')
def workspace(tmp_path_factory):
    root = tmp_path_factory.mktemp('workflow') / 'project'
    p = Project('projects/npc-behavior-1').clone(root)
    return root, copy.deepcopy(p.doc)


@pytest.fixture
def p(workspace):
    root, doc = workspace
    atomic_json(root / 'project.json', doc)
    yield Project(root)
    atomic_json(root / 'project.json', doc)


def apply(p, actions):
    return p.apply_workflow(p.doc['revision'], actions)


def isolate(p):
    apply(p, [action('interior', {})])


def floor(x=8, z=7, material='lambert12'):
    return action('paint', {'cells': [{'x': x, 'z': z}], 'material': material})


def test_room_copy_preview_reopen_undo_redo_export_isolation(p, tmp_path):
    before = p.path.read_bytes()
    plan = p.plan_workflow([action('interior', {})])
    q = p.area_preview_project(plan)
    assert p.path.read_bytes() == before
    assert p.context(**ROOM)['map_member'] == 215
    assert q.context(**ROOM)['map_member'] == 676
    assert q.context(header=70, cell=[0, 0])['map_member'] == 215
    apply(p, [action('interior', {}), floor()])
    q = Project(p.root)
    assert q.context(**ROOM)['matrix']['id'] == 288
    assert q.contexts(header=72)['cells'][0]['map_member'] == 676
    assert q.map_neighborhood(**ROOM, image=False)['cells'][0]['map_member'] == 676
    q.export(tmp_path / 'new', q.doc['revision'])
    original = historical_rom('projects/npc-behavior-1/exports/behavior-r22/game.nds')
    exported = (tmp_path / 'new/game.nds').read_bytes()
    a, b = ndspy.rom.NintendoDSRom(original), ndspy.rom.NintendoDSRom(exported)
    for archive, addition in ((world.MAP_ARCHIVE, 1), (world.MATRIX_ARCHIVE, 1)):
        old = ndspy.narc.NARC(a.getFileByName(archive)).files
        new = ndspy.narc.NARC(b.getFileByName(archive)).files
        assert len(new) == len(old) + addition and new[:len(old)] == old
    for fid, data in enumerate(a.files):
        if a.filenames.filenameOf(fid) not in (world.MAP_ARCHIVE, world.MATRIX_ARCHIVE):
            assert b.files[fid] == data
    arm = bytearray(b.arm9)
    offset = world.HEADER_TABLE + 72 * world.HEADER_SIZE + 4
    assert struct.unpack_from('<H', arm, offset)[0] == 288
    arm[offset:offset + 2] = a.arm9[offset:offset + 2]
    assert bytes(arm) == a.arm9
    q.undo(q.doc['revision']); q = Project(q.root)
    assert q.context(**ROOM)['map_member'] == 215
    q.export(tmp_path / 'undo', q.doc['revision'])
    assert (tmp_path / 'undo/game.nds').read_bytes() == original
    q.redo(q.doc['revision']); q = Project(q.root)
    q.export(tmp_path / 'redo', q.doc['revision'])
    assert (tmp_path / 'redo/game.nds').read_bytes() == exported


def test_interior_refusals_and_tamper_never_write(p):
    before = p.path.read_bytes()
    with pytest.raises(EditorError):
        apply(p, [action('interior', {}, CG)])
    assert p.path.read_bytes() == before
    isolate(p); before = p.path.read_bytes()
    with pytest.raises(EditorError):
        isolate(p)
    assert p.path.read_bytes() == before
    doc = copy.deepcopy(p.doc); doc['map_edits'][-1]['map_member'] += 1
    atomic_json(p.path, doc)
    with pytest.raises(EditorError):
        Project(p.root)


def test_multiple_rooms_have_distinct_allocations(p):
    apply(p, [action('interior', {}), action('interior', {}, {'header': 70, 'cell': [0, 0]})])
    q = Project(p.root)
    assert q.context(**ROOM)['map_member'] == 676
    assert q.context(header=70, cell=[0, 0])['map_member'] == 677
    assert q.context(header=203, cell=[0, 0])['map_member'] == 215


def test_saved_group_move_repair_links_reopen_and_guard(p):
    isolate(p)
    objects = [f'baseline:676:{i}' for i in range(4, 9)]
    cells = [{'x': x, 'z': z} for x in range(5, 9) for z in range(5, 7)]
    blocked = [c for c in cells if p.permission_cells(**ROOM, **c)['cells'][0]['collision'] & 128]
    group = action('group', {'action': 'save', 'name': 'Dining set', 'object_ids': objects,
        'cells': blocked, 'events': [{'kind': 'npc', 'event_id': i} for i in (0, 1)],
        'repair': {'cells': cells, 'material': 'lambert9'}})
    apply(p, [group, action('layout', {'group': 'Dining set', 'action': 'move', 'dx': -2, 'dz': 0})])
    q = Project(p.root); g = q.linked_groups(**ROOM)[0]
    assert {o['id'] for o in g['objects']} == set(objects)
    assert [(e['x'], e['z']) for e in g['events']] == [(3, 5), (6, 5)]
    assert {c['x'] for c in g['repair']['cells']} == {3, 4, 5, 6}
    before = q.path.read_bytes()
    with pytest.raises(EditorError):
        q.apply_scenery_edit(q.doc['revision'], **ROOM, operation='move', slot=4, x=6, z=6)
    assert q.path.read_bytes() == before
    apply(q, [action('layout', {'group': 'Dining set', 'action': 'move', 'dx': 1, 'dz': 0})])
    assert q.linked_groups(**ROOM)[0]['objects'][0]['x'] == 6


def test_staging_remove_adjust_undo_redo_cancel_and_stale(p):
    session = WorkflowSession(p); original = p.path.read_bytes()
    session.stage(action('interior', {})); session.stage(floor())
    assert session.preview.context(**ROOM)['map_member'] == 676 and p.path.read_bytes() == original
    session.replace(1, floor(x=7)); session.remove(1)
    assert len(session.actions) == 1
    session.stage(floor()); session.undo(); session.redo()
    assert len(session.actions) == 2 and p.path.read_bytes() == original
    p.apply_area_edit(p.doc['revision'], operations=[{'kind': 'event', 'context': CG, 'request': {'kind': 'npc', 'event_id': 2, 'values': {'facing': 2}}}])
    newer = p.path.read_bytes()
    with pytest.raises(EditorError) as exc:
        session.apply()
    assert exc.value.code == 'STALE_REVISION' and p.path.read_bytes() == newer


def test_invalid_queue_replacement_retains_prior_preview(p):
    session = WorkflowSession(p); session.stage(action('interior', {})); session.stage(floor())
    previous = copy.deepcopy(session.actions)
    with pytest.raises(EditorError):
        session.replace(1, floor(x=25, z=25))
    assert session.actions == previous


def test_durable_redo_branch_and_noop(p):
    isolate(p); p.undo(p.doc['revision']); old_revision = p.doc['revision']
    q = Project(p.root); assert q.doc.get('redo')
    before = q.path.read_bytes()
    q.apply_area_edit(q.doc['revision'], operations=[{'kind': 'event', 'context': CG, 'request': {'kind': 'npc', 'event_id': 2, 'values': {}}}])
    assert q.path.read_bytes() == before
    apply(q, [action('paint', {'cells': [{'x': 557, 'z': 403}], 'material': 'grass01'}, CG)])
    assert q.doc['revision'] == old_revision + 1 and not q.doc.get('redo')
    with pytest.raises(EditorError):
        q.redo(q.doc['revision'])


def test_eyedropper_and_stroke_bounds(p):
    sampled = p.sample_surface(2, 6, **ROOM)
    assert sampled['material'] == 'lambert9'
    with pytest.raises(EditorError):
        p.sample_surface(25, 25, **ROOM)
    original = p.path.read_bytes()
    cells = [{'x': x, 'z': z} for x in range(1, 10) for z in range(1, 9)]
    with pytest.raises(EditorError):
        apply(p, [action('paint', {'cells': cells, 'material': 'lambert9'})])
    assert p.path.read_bytes() == original


def test_redo_snapshot_tamper_refuses(p):
    isolate(p); p.undo(p.doc['revision']); doc = copy.deepcopy(p.doc)
    doc['redo'][-1]['snapshot']['map_edits'][-1]['map_member'] += 1
    atomic_json(p.path, doc); q = Project(p.root); before = q.path.read_bytes()
    with pytest.raises(EditorError):
        q.redo(q.doc['revision'])
    assert q.path.read_bytes() == before


def test_sampled_floor_uv_mapping_roundtrip_and_undo(p):
    import numpy as np
    from sovereign_editor import mapscene, nitro, surface_authoring
    isolate(p)
    sample = p.sample_surface(2, 6, **ROOM)
    a = action('paint', {'cells': [{'x': 8, 'z': 8}], 'material': 'lambert9', 'sample': {'x': 2, 'z': 6}})
    apply(p, [a]); q = Project(p.root); ctx = q.context(**ROOM)
    t = q.doc['map_edits'][-1]
    assert t['schema'] == surface_authoring.SAMPLED_SCHEMA
    assert t['cells'][0]['after']['affine'] == sample['affine']
    _, blobs = mapscene.tilesets(q,ctx)
    _, prims = nitro.decode_model(surface_authoring.model(q,ctx,q.composed()),tileset=blobs['map_tileset'])
    affine = np.array(sample['affine']).reshape(3,2); found = []
    for prim in prims:
        if prim.material['name'] != 'lambert9': continue
        for tri in prim.triangles:
            v = prim.vertices[tri]
            if np.all(v[:,0]>=-128.001) and np.all(v[:,0]<=-111.999) and np.all(v[:,2]>=-128.001) and np.all(v[:,2]<=-111.999):
                expected = np.column_stack([v[:,0],v[:,2],np.ones(3)]) @ affine
                assert np.max(np.abs(expected-prim.uvs[tri])) <= 1/(16*min(prim.texture.shape[:2])) + .0001
                found.append(tri)
    assert found
    q.undo(q.doc['revision']); assert not q.composed().get('surfaces',{}).get(676)


def test_group_destination_cannot_take_unrelated_blocker(p):
    before = p.path.read_bytes()
    # Table into the blocked shelf: it must refuse, even though both anchors have a flat floor.
    with pytest.raises(EditorError):
        apply(p, [action('layout', {'action': 'move', 'object_ids': ['baseline:215:4'], 'dx': 1, 'dz': -2,
            'cells': [{'x': 6, 'z': 5}, {'x': 7, 'z': 5}, {'x': 6, 'z': 6}, {'x': 7, 'z': 6}]})])
    assert p.path.read_bytes() == before


def test_preview_validation_keeps_saved_reads_and_room_context(p):
    isolate(p)
    before = p.path.read_bytes()
    events = p.map_events(**ROOM)
    original = next(e for e in events['events'] if e['kind'] == 'npc' and e['id'] == 0)
    plan = p.plan_event_edit(**ROOM, kind='npc', event_id=0, values={'facing': 0 if original['facing'] else 1})
    assert not plan['empty']
    assert p.map_events(**ROOM) == events
    assert p.context(**ROOM)['map_member'] == 676
    # Undo validation temporarily composes a state without the private room.
    p._validate_state(p.doc['history'][-1])
    assert p.context(**ROOM)['map_member'] == 676
    assert p.map_events(**ROOM) == events and p.path.read_bytes() == before


def test_cli_workspace_preview_apply_history_and_invalid_root(p, tmp_path, capsys):
    from sovereign_editor.cli import main
    request = tmp_path / 'actions.json'
    request.write_text(json.dumps({'actions': [action('interior', {})]}))
    args = ['workspace-edit', '--project', str(p.root), '--request', str(request)]
    before = p.path.read_bytes()
    assert main(args + ['--dry-run']) == 0
    assert json.loads(capsys.readouterr().out)['result']['actions'][0]['kind'] == 'interior'
    assert p.path.read_bytes() == before
    assert main(args + ['--revision', str(p.doc['revision'])]) == 0
    assert json.loads(capsys.readouterr().out)['result']['changed']
    q = Project(p.root)
    for command, member in [('undo', 215), ('redo', 676)]:
        assert main([command, '--project', str(q.root), '--revision', str(q.doc['revision'])]) == 0
        assert json.loads(capsys.readouterr().out)['ok']
        q = Project(q.root); assert q.context(**ROOM)['map_member'] == member
    before = p.path.read_bytes(); request.write_text('[]')
    assert main(args + ['--dry-run']) == 2
    assert json.loads(capsys.readouterr().out)['error']['code'] == 'INVALID_INPUT'
    assert p.path.read_bytes() == before
