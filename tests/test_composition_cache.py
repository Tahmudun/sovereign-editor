"""PROD-PERF-001: content-keyed composition snapshots, isolated trials, identical results.

A snapshot is reused only for byte-identical composition inputs (positions,
placement moves, map edits), the same verified baseline and the same editor
code; it is signed with a per-user key, so a tampered or foreign file is a miss.
"""
import copy
import json
import pickle
from pathlib import Path

import pytest

from sovereign_editor import core, snapshots
from sovereign_editor.core import Project, atomic_json
from sovereign_editor.formats import EditorError

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / 'projects/scyther-orchestration-1/baseline.nds'
CTX = {'header': 33, 'cell': [18, 12]}


def field(identity='survey_field', name='SURVEY_FIELD'):
    return {'kind': 'world', 'context': CTX, 'request': {
        'action': 'create', 'identity': identity, 'name': 'Survey Field', 'internal_name': name,
        'template_header': 33, 'encounters': 'none', 'worldmap': [19, 12],
        'cells': [{'cell': [0, 0], 'source': {'header': 33, 'cell': [18, 12]}}]}}


def state_op(key, switch=False):
    value = {'name': key, **({'switch': True} if switch else {})}
    return {'kind': 'story', 'context': CTX, 'request': {'kind': 'state', 'key': key, 'value': value}}


@pytest.fixture(scope='module')
def workspace(tmp_path_factory):
    if not BASELINE.exists():
        pytest.skip('Pinned baseline is local and untracked')
    root = tmp_path_factory.mktemp('cache')
    snapshots.configure(root / 'user-cache')
    p = Project.create(BASELINE, root / 'project')
    p.apply_area_edit(0, operations=[field(), state_op('alpha'), state_op('beta', True)])
    return p.root, copy.deepcopy(p.doc), root / 'user-cache'


@pytest.fixture
def p(workspace, monkeypatch):
    root, doc, cache = workspace
    snapshots.configure(cache)
    atomic_json(root / 'project.json', copy.deepcopy(doc))
    return Project(root)


def fingerprint(project):
    """Everything composition leaves on a Project, order-sensitive."""
    return snapshots.structure_digest(project)


def full(project, state=None):
    """Independent from-scratch composition + validation of a state (no cache)."""
    fresh = Project.__new__(Project)
    fresh.__dict__.update(project.__dict__)
    fresh._snapshot_memory = {}
    with snapshots.disabled():
        fresh._validate_composed_state(project.doc if state is None else state)
    return fresh


def compose_calls(monkeypatch):
    calls = []
    original = core.Project._compose

    def counted(self, state):
        calls.append(1)
        return original(self, state)
    monkeypatch.setattr(core.Project, '_compose', counted)
    return calls


def test_fresh_process_open_reuses_a_signed_snapshot(p, monkeypatch):
    calls = compose_calls(monkeypatch)
    snapshots.clear_memory()
    reopened = Project(p.root)
    assert not calls                                            # loaded, not replayed
    assert fingerprint(reopened) == fingerprint(full(reopened))
    assert reopened.composed()['story']['state']['beta']['switch']


def test_tampered_snapshot_is_ignored(p, monkeypatch):
    for path in snapshots.directory().glob('*.snapshot'):
        raw = bytearray(path.read_bytes()); raw[-1] ^= 1; path.write_bytes(bytes(raw))
    snapshots.clear_memory()
    calls = compose_calls(monkeypatch)
    reopened = Project(p.root)
    assert calls                                                # recomposed on the miss
    assert fingerprint(reopened) == fingerprint(full(reopened))


def test_changed_inputs_or_code_miss_the_cache(p, monkeypatch):
    doc = json.loads(p.path.read_text())
    doc['map_edits'][-1]['label'] = 'Tampered label'
    p.path.write_text(json.dumps(doc))
    snapshots.clear_memory()
    with pytest.raises(EditorError):
        Project(p.root)                                         # full validation refuses it
    atomic_json(p.path, json.loads(json.dumps(p.doc)))
    monkeypatch.setattr(snapshots, 'CODE', 'other-editor-build')
    snapshots.clear_memory()
    calls = compose_calls(monkeypatch)
    Project(p.root)
    assert calls


def test_plan_is_isolated_and_equals_full_composition(p):
    before = fingerprint(p)
    plan = p.plan_area_edit([field('second_field', 'SECOND_FIELD'), state_op('gamma', True)])
    assert fingerprint(p) == before and 541 not in p._world_headers
    trial = p.area_preview_project(plan)
    assert 541 in trial._world_headers and 541 not in p._world_headers
    assert fingerprint(trial) == fingerprint(full(trial))


def test_apply_undo_redo_match_full_composition_and_reuse_snapshots(p, monkeypatch):
    p.apply_area_edit(p.doc['revision'], operations=[state_op('delta')])
    assert fingerprint(p) == fingerprint(full(p))
    calls = compose_calls(monkeypatch)
    p.undo(p.doc['revision'])
    undone = fingerprint(p)
    assert not calls and 'delta' not in p.composed()['story']['state']    # validated before: reused
    assert undone == fingerprint(full(p))
    calls.clear()
    p.redo(p.doc['revision'])
    redone = fingerprint(p)
    assert not calls and 'delta' in p.composed()['story']['state']
    assert redone == fingerprint(full(p))


def test_stale_revision_is_still_refused(p):
    other = Project(p.root)
    p.apply_area_edit(p.doc['revision'], operations=[state_op('epsilon')])
    with pytest.raises(EditorError) as exc:
        other.apply_area_edit(other.doc['revision'], operations=[state_op('zeta')])
    assert exc.value.code == 'STALE_REVISION'
