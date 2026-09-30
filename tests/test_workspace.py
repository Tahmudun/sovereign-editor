"""Workspace (WORKSPACE-01..03) and reach (ACCESS-01) — software checks only."""
import contextlib
import io
import json
from pathlib import Path

import pytest

from sovereign_editor import cli, workspace as ws, save_read
from sovereign_editor.core import Project
from sovereign_editor.formats import EditorError, digest

ROOT = Path(__file__).resolve().parents[1]
PARENT = ROOT / 'projects/assets-gameplay-v1'
SAVE_COPY = ROOT / 'work/editor-completion-v1/access/current-save-copy.sav'
pytestmark = pytest.mark.skipif(not PARENT.is_dir(), reason='returned assets-gameplay-v1 (r64) parent absent')


def run_cli(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cli.main([str(a) for a in argv])
    return json.loads(out.getvalue())


def data_op(ops):
    return {'kind': 'data', 'context': {'header': 33, 'cell': [19, 12]}, 'request': {'operations': ops, 'label': 'data'}}


def test_index_cross_links_search_and_references():
    p = Project(PARENT)
    found = p.workspace_search('robin')
    refs = {e['ref'] for e in found['entries']}
    assert {'event:robin', 'trainer:robin', 'state:robin_defeated'} <= refs
    state = p.workspace_references('state:robin_defeated')
    assert {u['ref'] for u in state['used_by']} >= {'event:robin', 'trainer:robin'} and state['shared']
    event = p.workspace_references('event:robin')
    assert {'trainer:robin', 'area:h33'} <= {u['ref'] for u in event['uses']}
    area = p.workspace_references('area:h542')
    assert 'terrain:542:terrace@98,63' in {u['ref'] for u in area['used_by']}
    everything = p.workspace_search('', limit=5)
    assert everything['counts']['character'] == 32 and everything['counts']['trainer'] == 64
    only = p.workspace_search('', kinds=['asset'])
    assert only['entries'] and all(e['kind'] == 'asset' for e in only['entries'])
    with pytest.raises(EditorError) as e:
        p.workspace_references('event:nope')
    assert e.value.code == 'NOT_FOUND'


def test_progression_grades_findings(monkeypatch):
    p = Project(PARENT)
    report = p.progression()
    levels = report['summary']
    assert levels.get('warning', 0) >= 124                      # production-load rooms: not connected
    assert any(f['level'] == 'unanalyzed' and f['area'] == 33 for f in report['findings'])
    assert all(f['level'] != 'error' for f in report['findings'])
    # A warp to a missing header is a definite error (injected: public operations refuse to write one).
    real = ws._warps

    def broken(project, state, header):
        rows = real(project, state, header)
        if header == 541:
            rows = [dict(rows[0], destination=9999)] + rows[1:]
        return rows
    monkeypatch.setattr(ws, '_warps', broken)
    errors = [f for f in ws.progression(p)['findings'] if f['level'] == 'error']
    assert errors and 'missing header 9999' in errors[0]['message']


@pytest.mark.skipif(not SAVE_COPY.is_file(), reason='copy of the current played save absent')
def test_reach_from_the_played_save_and_with_guides(tmp_path):
    before = digest(SAVE_COPY.read_bytes())
    saved = save_read.read(SAVE_COPY)
    assert (saved['header'], saved['x'], saved['z'], saved['facing']) == (33, 629, 411, 'south')
    parent = Project(PARENT).reach(save=SAVE_COPY)
    assert '67' not in parent['by_header'] and parent['by_header']['33'] > 400        # WORLD-ACCESS-001
    import sys
    sys.path.insert(0, str(ROOT / 'tests'))
    from test_editor_completion import ACCESS
    p = Project(PARENT).clone(tmp_path / 'p')
    p.apply_area_edit(64, operations=ACCESS, label='access')
    after = p.reach(save=SAVE_COPY)
    assert after['by_header']['67'] > 400 and after['reached'] > parent['reached']
    travel = [l for l in after['links'] if l['kind'] == 'travel']
    assert {(l['from'][0], l['to']) for l in travel} == {(33, 67), (67, 33)} and not any(l['conditions'] for l in travel)
    assert digest(SAVE_COPY.read_bytes()) == before                                    # read only


def test_impact_report_names_changes_and_shared_users(tmp_path):
    p = Project(PARENT).clone(tmp_path / 'p')
    state = p.composed()
    from sovereign_editor import game_data as gd
    potion = gd._members(p, state, gd.ITEMS)(17)
    ops = [data_op([{'kind': 'shop', 'name': 'ws_herbs', 'before': None, 'items': [17, 18]},
                    {'kind': 'item', 'id': 17, 'before_sha256': digest(potion), 'changes': {'price': 250}}])]
    before = p.path.read_bytes()
    report = p.impact(ops, label='shop and price')
    assert p.path.read_bytes() == before
    rows = {r['ref']: r for r in report['affected']}
    assert rows['shop:ws_herbs']['change'] == 'added'
    assert rows['record:item 17']['change'] in ('added', 'changed') and 'shop:ws_herbs' in rows['record:item 17']['shared_by']
    request = tmp_path / 'r.json'
    request.write_text(json.dumps({'operations': ops, 'label': 'cli'}))
    assert run_cli('impact-report', '--project', p.root, '--request', request)['result']['transactions'] == 1
    assert run_cli('workspace-refs', '--project', p.root, '--ref', 'event:robin')['ok']
    assert run_cli('workspace-search', '--project', p.root, '--query', 'cut', '--kind', 'event')['ok']
    assert run_cli('progression-report', '--project', p.root)['result']['summary']


def test_project_browser_ui(tmp_path):
    import os
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtWidgets import QApplication
    from sovereign_editor.map_inspector import MapInspectorWindow
    from sovereign_editor.workspace_ui import ProjectBrowser
    from sovereign_editor.gui import STYLE
    app = QApplication.instance() or QApplication([]); app.setStyle('Fusion'); app.setStyleSheet(STYLE)
    p = Project(PARENT).clone(tmp_path / 'p')
    w = MapInspectorWindow(p, context=(33, [19, 12])); w.show(); app.processEvents()
    menu = w.project_menu
    assert menu.menuAction() in w.menuBar().actions() and menu.title() == 'Project'
    entries = {a.text(): a.shortcut().toString() for a in menu.actions()}
    assert entries == {'Project browser…': 'Ctrl+B', 'Checkpoints and packages…': 'Ctrl+Shift+K'}
    next(a for a in menu.actions() if a.text() == 'Project browser…').trigger(); app.processEvents()
    assert isinstance(w.browser, ProjectBrowser) and w.browser.isVisible()
    w.browser.close()
    b = ProjectBrowser(w); b.show(); app.processEvents()
    b.query.setText('robin'); app.processEvents()
    refs = [b.results.item(i).data(0x0100) for i in range(b.results.count())]
    b.results.setCurrentRow(refs.index('state:robin_defeated')); app.processEvents()
    assert b.used_by.count() >= 2 and 'Shared' in b.detail.text()
    b.jump('event:robin'); app.processEvents()
    assert b.current_ref() == 'event:robin' and b.uses.count() >= 2
    out = ROOT / 'evidence/editor-completion-v1/ui'; out.mkdir(parents=True, exist_ok=True)
    b.resize(1100, 760); app.processEvents(); assert b.grab().save(str(out / 'browser-links.png'))
    b.tabs.setCurrentIndex(1); b.run_progression(); app.processEvents()
    assert b.findings.count() > 100 and b.edges.count() > 0
    b.level.setCurrentIndex(b.level.findData('unanalyzed')); app.processEvents()
    assert 0 < b.findings.count() < 10
    assert b.grab().save(str(out / 'browser-progression.png'))
    if SAVE_COPY.is_file():
        b.tabs.setCurrentIndex(2); b.save_path.setText(str(SAVE_COPY)); b.run_reach(); app.processEvents()
        assert 'header 33 tile 629,411' in b.reach_out.toPlainText()
        b.resize(1000, 720); app.processEvents(); assert b.grab().save(str(out / 'browser-reach-compact.png'))
    b.close(); w.close(); app.processEvents()


def test_mixed_batch_is_atomic_stale_refused_and_undoable(tmp_path):
    p = Project(PARENT).clone(tmp_path / 'p')
    shop = data_op([{'kind': 'shop', 'name': 'ws_mixed', 'before': None, 'items': [17]}])
    clerk = {'kind': 'story', 'context': {'header': 33, 'cell': [19, 12]}, 'request': {
        'kind': 'sequence', 'key': 'ws_clerk', 'action': 'put', 'value': {
            'kind': 'npc', 'x': 634, 'z': 410, 'donor_id': 0, 'facing': 1, 'movement': 0, 'range_x': 0, 'range_z': 0,
            'character': None, 'stock_sprite': 333, 'once_state': None,
            'nodes': [{'id': 'buy', 'op': 'shop', 'shop': 'ws_mixed', 'next': 'done'},
                      {'id': 'done', 'op': 'end', 'complete': False}]}}}
    pond = {'kind': 'elevation', 'context': {'header': 542, 'cell': [3, 2]},
            'request': {'action': 'water_shape', 'rects': [[107, 77, 3, 7], [99, 82, 8, 2]], 'traversable': True}}
    bad = dict(clerk, request=dict(clerk['request'], key='ws_bad',
                                   value=dict(clerk['request']['value'], x=638, z=409)))   # blocked tile
    before = p.path.read_bytes()
    with pytest.raises(EditorError):
        p.apply_area_edit(64, operations=[shop, clerk, pond, bad])                       # one failure: nothing written
    assert p.path.read_bytes() == before
    result = p.apply_area_edit(64, operations=[shop, clerk, pond], label='mixed')
    assert result['revision'] == 65
    impact = {r['ref'] for r in p.impact([data_op([{'kind': 'shop', 'name': 'ws_mixed', 'before': [17],
                                                     'items': [17, 18]}])])['affected']}
    assert 'shop:ws_mixed' in impact
    with pytest.raises(EditorError) as e:
        p.apply_area_edit(64, operations=[shop])                                          # stale revision
    assert e.value.code in ('STALE_REVISION', 'REVISION_MISMATCH', 'STALE'), e.value.code
    p.undo(65)
    assert 'ws_clerk' not in p.story_library()['sequences'] and not p.workspace_search('ws_mixed')['total']
    p.redo(66)
    assert p.workspace_references('shop:ws_mixed')['used_by'][0]['ref'] == 'event:ws_clerk'


def test_coverage_table_is_one_source_for_app_cli_and_docs(tmp_path):
    """RELEASE-02: the app, the CLI and docs/EDITOR_V1_COVERAGE.md show the same table and live limits."""
    import os
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtWidgets import QApplication
    from sovereign_editor import coverage, world_runtime as wr
    from sovereign_editor.map_inspector import MapInspectorWindow
    from sovereign_editor.workspace_ui import ProjectBrowser
    rows = coverage.table()
    assert {r['status'] for r in rows} <= {coverage.SUPPORTED, coverage.LIMITED, coverage.READ_ONLY, coverage.UNSUPPORTED}
    assert all(r['limits'] for r in rows if r['status'] != coverage.SUPPORTED)
    p = Project(PARENT).clone(tmp_path / 'p')
    report = run_cli('coverage', '--project', p.root)['result']
    assert report['rows'] == rows and sum(report['counts'].values()) == len(rows)
    assert report['limits']['created_headers']['limit'] == wr.MAX_HEADERS
    assert report['limits']['authored_shops']['limit'] == 32 and report['revision'] == p.doc['revision']
    docs = (ROOT / 'docs/EDITOR_V1_COVERAGE.md').read_text()
    table = coverage.markdown().splitlines()[4:]
    assert docs.splitlines()[4:4 + len(table)] == table   # generated docs match the code's table
    app = QApplication.instance() or QApplication([])
    w = MapInspectorWindow(p, context=(33, [19, 12])); b = ProjectBrowser(w)
    assert b.coverage_table.rowCount() == len(rows)
    assert [b.coverage_table.item(i, 2).text() for i in range(len(rows))] == [r['status'] for r in rows]
    b.close(); w.close(); app.processEvents()
