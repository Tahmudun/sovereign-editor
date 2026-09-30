"""Base-ROM repairs (R101-SHOP): the mart Cancel/Exit icon request, the Project operation and
the exported archive. CPU/readback evidence only; the touch exit itself is a native check."""
import hashlib
import struct
import sys
from pathlib import Path

import pytest

from sovereign_editor import runtime_repairs as rr, world_authoring
from sovereign_editor.core import Project
from sovereign_editor.formats import EditorError, member_count, resource

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / 'projects/original-content-v1/baseline.nds'
pytestmark = pytest.mark.skipif(not BASELINE.is_file(), reason='pinned baseline absent')
sys.path[:0] = [str(ROOT / 'tools')]
CONTEXT = {'header': 67, 'cell': [16, 12]}


def edit(p, **request):
    return p.apply_area_edit(p.doc['revision'], operations=[{'kind': 'repair', 'context': CONTEXT, 'request': request}])


@pytest.fixture(scope='module')
def clean(tmp_path_factory):
    return Project.create(BASELINE, tmp_path_factory.mktemp('repairs') / 'p', name='Repair clean')


def test_baseline_defect_is_reproduced_on_the_cpu():
    import shop_exit_qualification as q
    blob = BASELINE.read_bytes()
    assert member_count(blob, rr.ICON_ARCHIVE) == rr.ICON_MEMBERS
    cancel = q.requested_members(blob, 0xFFFF)
    assert [(c['call'], c['member']) for c in cancel] == [('char', rr.RETURN_CHAR), ('pltt', rr.RETURN_PLTT)]
    assert all(c['member'] >= rr.ICON_MEMBERS for c in cancel)          # past the archive's end
    potion = q.requested_members(blob, 17)
    assert [c['member'] for c in potion] == [36, 37]                    # item i -> 2i+2 / 2i+3


def test_icon_encoding_matches_the_base_hack_build():
    blob = BASELINE.read_bytes()
    # The same encoder reproduces an existing icon member exactly (none.png is item 0's icon).
    char, pltt = resource(blob, rr.ICON_ARCHIVE, 2)[1], resource(blob, rr.ICON_ARCHIVE, 3)[1]
    members = rr.members(blob)
    assert members[0] == char and members[1] == pltt
    assert members[2][:48] == char[:48] and members[3][:40] == pltt[:40] and members[3][72:] == pltt[72:]
    assert members[2] != char and len(members[2]) == 560 and len(members[3]) == 552


def test_operation_refusals_undo_reopen_and_view(clean):
    p = clean
    before = p.path.read_bytes()
    for request, message in (({'action': 'fix', 'repair': 'mart_return_icon'}, 'action'),
                             ({'action': 'apply', 'repair': 'nope'}, 'Repair is one of'),
                             ({'action': 'remove', 'repair': 'mart_return_icon'}, 'not applied')):
        with pytest.raises(EditorError, match=message):
            edit(p, **request)
    assert p.path.read_bytes() == before
    view = p.repair_view()['repairs']
    assert [(r['repair'], r['applied'], r['available']) for r in view] == [('mart_return_icon', False, True)]
    edit(p, action='apply', repair='mart_return_icon')
    with pytest.raises(EditorError, match='already applied'):
        edit(p, action='apply', repair='mart_return_icon')
    assert Project(p.root).repair_view()['repairs'][0]['applied']
    plan = world_authoring.runtime_plan(p, p.composed())
    assert len(plan['appends'][rr.ICON_ARCHIVE]) == 4
    summary = [s for s in p.diff() if s.get('operation') == 'runtime.repair']
    assert summary[0]['report']['appended'] == [5396, 5397, 5398, 5399]
    p.undo(p.doc['revision'])
    assert not rr.applied(p.composed())
    assert rr.ICON_ARCHIVE not in world_authoring.runtime_plan(p, p.composed()).get('appends', {})
    p.redo(p.doc['revision'])
    assert rr.applied(p.composed()) == {'mart_return_icon'}


def test_export_appends_the_return_icon(clean, tmp_path):
    import shop_exit_qualification as q
    p = clean
    if not rr.applied(p.composed()):
        edit(p, action='apply', repair='mart_return_icon')
    p.export(tmp_path / 'out', p.doc['revision'])
    rom = (tmp_path / 'out' / 'game.nds').read_bytes()
    base = BASELINE.read_bytes()
    assert member_count(rom, rr.ICON_ARCHIVE) == rr.ICON_MEMBERS + 4
    for m in (0, 1, 2, 3, 36, 37, 5394, 5395):
        assert resource(rom, rr.ICON_ARCHIVE, m)[1] == resource(base, rr.ICON_ARCHIVE, m)[1]
    report = [c for c in q.requested_members(rom, 0xFFFF)]
    assert all(c['member'] < member_count(rom, rr.ICON_ARCHIVE) for c in report)
    assert [resource(rom, rr.ICON_ARCHIVE, m)[1] for m in (5396, 5397, 5398, 5399)] == rr.members(base)
    assert hashlib.sha256(rr.ASSET.read_bytes()).hexdigest() == rr.ASSET_SHA256
