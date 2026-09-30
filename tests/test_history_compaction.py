"""PROD-PERF-001: history entries store map edits as a delta against the state after them,
so project.json no longer grows by the whole edit list per edit. Undo/Redo stay exact."""
import copy
import json
from pathlib import Path

import pytest

from sovereign_editor.core import Project, atomic_json
from sovereign_editor.formats import EditorError

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / 'projects/scyther-orchestration-1/baseline.nds'
CONTEXT = {'header': 33, 'cell': [19, 12]}


def state_op(key):
    return [{'kind': 'story', 'context': CONTEXT,
             'request': {'kind': 'state', 'key': key, 'value': {'name': key.title()}}}]


@pytest.fixture(scope='module')
def workspace(tmp_path_factory):
    if not BASELINE.exists():
        pytest.skip('Pinned baseline is local and untracked')
    root = tmp_path_factory.mktemp('history')
    p = Project.create(BASELINE, root / 'project')
    return p.root, copy.deepcopy(p.doc)


@pytest.fixture
def p(workspace):
    root, doc = workspace
    atomic_json(root / 'project.json', copy.deepcopy(doc))
    return Project(root)


def test_new_entries_are_deltas_and_undo_redo_restore_exactly(p):
    states = [copy.deepcopy(p.doc['map_edits'])]
    for key in ('alpha', 'beta', 'gamma'):
        p.apply_area_edit(p.doc['revision'], operations=state_op(key))
        states.append(copy.deepcopy(p.doc['map_edits']))
    assert all('map_edits' not in h and h['map_edits_delta']['tail'] == [] for h in p.doc['history'])
    assert [h['map_edits_delta']['keep'] for h in p.doc['history']] == [len(s) for s in states[:-1]]
    q = Project(p.root)                                      # the stored form, reopened
    for expected in reversed(states[:-1]):
        q.undo(q.doc['revision'])
        assert q.doc['map_edits'] == expected
    for expected in states[1:]:
        q.redo(q.doc['revision'])
        assert q.doc['map_edits'] == expected
    assert Project(p.root).doc['map_edits'] == states[-1]


def test_a_divergent_edit_after_undo_keeps_the_undone_tail(p):
    p.apply_area_edit(p.doc['revision'], operations=state_op('alpha'))
    p.apply_area_edit(p.doc['revision'], operations=state_op('beta'))
    p.undo(p.doc['revision'])
    before = copy.deepcopy(p.doc['map_edits'])
    p.apply_area_edit(p.doc['revision'], operations=state_op('delta'))
    p.undo(p.doc['revision'])
    assert p.doc['map_edits'] == before
    p.undo(p.doc['revision'])
    assert p.doc['map_edits'] == []


def test_full_legacy_entries_still_undo(p):
    p.apply_area_edit(p.doc['revision'], operations=state_op('alpha'))
    p.apply_area_edit(p.doc['revision'], operations=state_op('beta'))
    doc = json.loads(p.path.read_text())
    first = doc['history'][0]
    first['map_edits'] = []
    del first['map_edits_delta']                              # the pre-v2 stored form
    atomic_json(p.path, doc)
    q = Project(p.root)
    q.undo(q.doc['revision'])
    q.undo(q.doc['revision'])
    assert q.doc['map_edits'] == []


def test_a_tampered_delta_is_refused(p):
    p.apply_area_edit(p.doc['revision'], operations=state_op('alpha'))
    doc = json.loads(p.path.read_text())
    doc['history'][-1]['map_edits_delta']['keep'] = 99
    atomic_json(p.path, doc)
    q = Project(p.root)
    before = q.path.read_bytes()
    with pytest.raises(EditorError) as exc:
        q.undo(q.doc['revision'])
    assert exc.value.code == 'BEFORE_VALUE_MISMATCH' and q.path.read_bytes() == before
