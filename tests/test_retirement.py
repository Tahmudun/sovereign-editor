"""Dependency-aware retirement of library definitions (PROD-02). Software checks only."""
from pathlib import Path

import pytest

from sovereign_editor import story_authoring as st
from sovereign_editor.core import Project
from sovereign_editor.formats import EditorError

ROOT = Path(__file__).resolve().parents[1]
PARENT = ROOT / 'projects/editor-completion-v1'
pytestmark = pytest.mark.skipif(not PARENT.is_dir(), reason='r82 parent absent')
CTX = {'header': 67, 'cell': [17, 12]}


def story(p, *requests):
    return p.apply_area_edit(p.doc['revision'], operations=[{'kind': 'story', 'context': CTX, 'request': r}
                                                           for r in requests])


def test_reference_refusal_retirement_fresh_allocation_reopen_and_undo(tmp_path):
    p = Project(PARENT).clone(tmp_path / 'p')
    state = p.composed()
    # Referenced definitions refuse, naming their users.
    with pytest.raises(EditorError, match='Still used by sequence beach_find'):
        story(p, {'kind': 'state', 'key': 'scyther_quest', 'action': 'retire'})
    with pytest.raises(EditorError, match='Still used by'):
        story(p, {'kind': 'character', 'key': 'tiana', 'action': 'retire'})
    with pytest.raises(EditorError, match='Retire takes'):
        story(p, {'kind': 'sequence', 'key': 'robin', 'action': 'retire'})
    number = st.catalog(state, 'state')['tiana_progress']
    trainer = st.catalog(state, 'trainer')['practice_01']
    story(p, {'kind': 'state', 'key': 'tiana_progress', 'action': 'retire'},
          {'kind': 'trainer', 'key': 'practice_01', 'action': 'retire'})
    state = p.composed()
    retired = st.catalog(state, 'state')['tiana_progress']
    assert retired['retired'] and retired['variable'] == number['variable']          # ID kept for saves
    assert st.catalog(state, 'trainer')['practice_01']['trainer_id'] == trainer['trainer_id']
    with pytest.raises(EditorError, match='already retired'):
        story(p, {'kind': 'state', 'key': 'tiana_progress', 'action': 'retire'})
    with pytest.raises(EditorError, match='retired; its ID stays reserved'):
        story(p, {'kind': 'state', 'key': 'tiana_progress', 'value': {'name': 'Again'}})
    # New definitions never reuse a retired ID and may not name retired definitions.
    story(p, {'kind': 'state', 'key': 'fresh_switch', 'value': {'name': 'Fresh', 'switch': True}})
    state = p.composed()
    flags = {v['flag'] for k, v in st.catalog(state, 'state').items() if v.get('switch') and k != 'fresh_switch'}
    assert st.catalog(state, 'state')['fresh_switch']['flag'] not in flags
    robin = st.catalog(state, 'sequence')['robin']
    value = {k: v for k, v in robin.items() if k in ('kind', 'x', 'z', 'donor_id', 'facing', 'movement', 'range_x',
                                                    'range_z', 'character', 'nodes', 'once_state')}
    value['once_state'] = 'tiana_progress'
    with pytest.raises(EditorError, match='Retired state cannot be used'):
        p.plan_area_edit([{'kind': 'story', 'context': {'header': robin['context']['header'], 'cell': robin['context']['cell']},
                           'request': {'kind': 'sequence', 'key': 'robin_again', 'value': value}}])
    # Capacity counts retired IDs as used: nothing is silently recycled.
    from sovereign_editor import capacity
    limits = capacity.report(p)['limits']
    assert limits['number_states']['used'] == 60 and limits['trainers']['used'] == 64
    reopened = Project(p.root)
    assert st.catalog(reopened.composed(), 'trainer')['practice_01']['retired']
    p.undo(p.doc['revision'])
    p.undo(p.doc['revision'])
    assert not st.catalog(p.composed(), 'state')['tiana_progress'].get('retired')
