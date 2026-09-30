"""R82-TUTOR-01 Able/Unable labels: tutor table, hooks and the script marker. Software checks only."""
import struct
from pathlib import Path

import pytest

from sovereign_editor import character_runtime as cr, dialogue_format as fmt, resident, story_authoring, tutor_labels as tl
from sovereign_editor import world_authoring
from sovereign_editor.core import Project

ROOT = Path(__file__).resolve().parents[1]
PARENT = ROOT / 'projects/editor-completion-v1'
pytestmark = pytest.mark.skipif(not PARENT.is_dir(), reason='r82 parent absent')


def test_tutor_rules_hooks_and_marker():
    p = Project(PARENT)
    state = p.composed()
    rows = tl.tutors(state)
    assert [(k, i, m, len(e)) for k, i, m, e in rows][:2] == [('demo_tutor', 'lesson', 196, 2), ('ec_hm_tutor', 'cut', 15, 0)]
    plan = world_authoring.runtime_plan(p, state)
    info = plan['tutor_labels']
    start = struct.unpack_from('<I', p.blob, 0x20)[0]
    for site, target in ((tl.DEFAULT_CALL, info['draw']), (tl.CHECK_CALL, info['check'])):
        patch = next(x for x in plan['patches'] if x['rom_offset'] == start + site - 0x02000000)
        assert bytes.fromhex(patch['after']) == cr.thumb_bl(site, target)
    image = plan['appends'][resident.BOOT_ARCHIVE][0]
    at = info['table'] - resident.BOOT_REGION[0]
    offsets = struct.unpack_from(f'<{len(rows)}I', image, at)
    for (key, node, move, refs), address in zip(rows, offsets):
        entry = image[address - resident.BOOT_REGION[0]:]
        assert struct.unpack_from('<HB', entry) == (move, len(refs))
        assert [struct.unpack_from('<HB', entry, 4 + 4 * k) for k in range(len(refs))] == refs
    ov129 = plan['files'][cr.overlay(p.blob, 129)['file_id']]
    for name, routine in (('draw', tl.draw_routine), ('check', tl.check_routine)):
        body = routine(info[name], len(rows), info['table'])
        assert ov129[info[name] - 0x023D8000:info[name] - 0x023D8000 + len(body)] == body
    # The demo tutor's script marks the selection (0xC000 | index) and clears the mark after it.
    replaced = story_authoring.replacements(p, state)
    seq = story_authoring.catalog(state, 'sequence')['demo_tutor']
    script = replaced[fmt.SCRIPT_ARCHIVE][seq['script_member']]
    mark, clear = struct.pack('<3H', 41, tl.MARK_VAR, tl.MARK | 0), struct.pack('<3H', 41, tl.MARK_VAR, 0)
    party_ui = struct.pack('<H', 349)
    i = script.index(mark)
    assert script.index(party_ui, i) < script.index(clear, i)
