"""Clean production project with explicitly reused content (PROD-01). Software checks only."""
from pathlib import Path

import pytest

from sovereign_editor import props, reuse, story_authoring as st
from sovereign_editor.core import Project
from sovereign_editor.formats import EditorError, digest

ROOT = Path(__file__).resolve().parents[1]
PARENT = ROOT / 'projects/editor-completion-v1'
pytestmark = pytest.mark.skipif(not PARENT.is_dir(), reason='r82 parent absent')
ITEMS = ['trainer:robin', 'trainer:tiana', 'prop:blossom']


def test_reuse_closure_own_ids_packages_refusals_reopen_and_undo(tmp_path):
    source = Project(PARENT)
    clean = Project.create(PARENT / 'baseline.nds', tmp_path / 'clean', name='Clean production')
    plan = reuse.plan(clean, source, ITEMS)
    assert [(r['kind'], r['key'], r['reason']) for r in plan['items']] == [
        ('state', 'robin_defeated', 'defeat state of trainer robin'),
        ('character', 'tiana', 'character of trainer tiana'),
        ('trainer', 'robin', 'selected'), ('trainer', 'tiana', 'selected'), ('prop', 'blossom', 'selected')]
    assert clean.path.read_bytes() == (tmp_path / 'clean/project.json').read_bytes()   # dry run writes nothing
    with pytest.raises(EditorError, match='no trainer nope'):
        reuse.plan(clean, source, ['trainer:nope'])
    with pytest.raises(EditorError, match='Reusable kinds'):
        reuse.plan(clean, source, ['sequence:robin'])
    before = clean.doc['revision']
    reuse.apply(clean, source, ITEMS, before)
    assert clean.doc['revision'] == before + 1
    state = clean.composed()
    assert {k: v['trainer_id'] for k, v in st.catalog(state, 'trainer').items()} == {'robin': 738, 'tiana': 739}
    assert list(st.catalog(state, 'character')) == ['tiana'] and list(st.catalog(state, 'state')) == ['robin_defeated']
    assert not state.get('world', {}).get('areas')                                  # no source areas or load rooms
    src_asset, asset = props.assets(source.composed())['blossom'], props.assets(state)['blossom']
    assert asset['package'] == src_asset['package'] and asset['revision'] == 1
    src_dir = props.package_dir(source, 'blossom', src_asset['package'])
    dst_dir = props.package_dir(clean, 'blossom', asset['package'])
    assert sorted(digest(p.read_bytes()) for p in src_dir.rglob('*') if p.is_file()) == \
        sorted(digest(p.read_bytes()) for p in dst_dir.rglob('*') if p.is_file())
    # Re-running reports what is already there and changes nothing.
    again = reuse.plan(clean, source, ITEMS)
    assert all(r['result'] == 'already in the clean project' for r in again['items']) and not again['operations']
    with pytest.raises(EditorError, match='already in this project'):
        reuse.apply(clean, source, ITEMS, clean.doc['revision'])
    reopened = Project(clean.root)
    assert st.catalog(reopened.composed(), 'trainer') == st.catalog(state, 'trainer')
    clean.undo(clean.doc['revision'])
    assert not st.catalog(clean.composed(), 'trainer')


def test_retired_source_definitions_are_not_reused(tmp_path):
    source = Project(PARENT).clone(tmp_path / 'src')
    source.apply_area_edit(source.doc['revision'], operations=[{'kind': 'story', 'context': reuse.CONTEXT,
        'request': {'kind': 'trainer', 'key': 'practice_01', 'action': 'retire'}}])
    clean = Project.create(PARENT / 'baseline.nds', tmp_path / 'clean', name='Clean')
    with pytest.raises(EditorError, match='retired in the source'):
        reuse.plan(clean, source, ['trainer:practice_01'])


def test_reuse_dialog(tmp_path):
    import os
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    from sovereign_editor.reuse_ui import ReuseDialog
    app = QApplication.instance() or QApplication([])
    clean = Project.create(PARENT / 'baseline.nds', tmp_path / 'clean', name='Clean')
    d = ReuseDialog(clean); d.show(); d.path.setText(str(PARENT)); d.guard(d.load); app.processEvents()
    keys = [d.items.item(i).data(Qt.ItemDataRole.UserRole) for i in range(d.items.count())]
    assert 'trainer:robin' in keys and not any(k.split(':')[1].startswith(('load_', 'practice_')) for k in keys)
    d.fixtures.setChecked(True); app.processEvents()
    assert any(d.items.item(i).data(Qt.ItemDataRole.UserRole).startswith('trainer:practice_')
               for i in range(d.items.count()))
    for i in range(d.items.count()):
        d.items.item(i).setSelected(d.items.item(i).data(Qt.ItemDataRole.UserRole) == 'trainer:robin')
    d.guard(d.preview_plan); app.processEvents()
    assert 'defeat state of trainer robin' in d.preview.toPlainText()
    d.guard(d.apply); app.processEvents()
    assert 'Applied' in d.status.text() and 'robin' in st.catalog(clean.composed(), 'trainer')
    d.close(); app.processEvents()
