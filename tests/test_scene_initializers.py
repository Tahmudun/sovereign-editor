"""Stage visibility must be set before object events spawn on warps and connections.

pret e97c7fc: sub_02053038 runs INIT_SCRIPT_ON_TRANSITION (2) before
Field_InitMapObjectsFromZoneEventData on warps (field_warp_tasks.c) and on map
connections (fieldmap.c FieldMap_ChangeZone); ON_LOAD (4) runs later and never
on a connection.
"""
import struct

import pytest

from sovereign_editor import dialogue_format as fmt, scene_authoring as sa
from sovereign_editor.formats import EditorError

STOCK_END = struct.pack('<H', 2)


def flag_code(c):
    c.emit('2H', 30, 0xB47)   # SetFlag, as the stage visibility code does


def goto_target(raw, at):
    op, delta = struct.unpack_from('<Hi', raw, at)
    assert op == 22
    return at + 6 + delta


def test_transition_script_is_prepended_when_the_map_has_none():
    raw = fmt.append_scripts(b'\x13\xfd', [STOCK_END])
    init, scripts = sa.chain_transition(b'\x00', raw, flag_code)
    assert init == struct.pack('<BI', 2, 2) + b'\x00'
    entries = fmt.script_entries(scripts)[1]
    assert scripts[entries[1]:] == struct.pack('<2HH', 30, 0xB47, 2)
    assert scripts[entries[0]:entries[0] + 2] == STOCK_END


def test_existing_transition_script_still_runs_after_stage_flags():
    stock = struct.pack('<3H', 41, 0x4000, 7) + STOCK_END
    raw = fmt.append_scripts(b'\x13\xfd', [STOCK_END, stock])
    table = struct.pack('<BI', 4, 1) + struct.pack('<BI', 2, 2) + b'\x00'
    init, scripts = sa.chain_transition(table, raw, flag_code)
    assert init == struct.pack('<BI', 4, 1) + struct.pack('<BI', 2, 3) + b'\x00'
    entries = fmt.script_entries(scripts)[1]
    body = entries[2]
    assert scripts[body:body + 4] == struct.pack('<2H', 30, 0xB47)
    assert goto_target(scripts, body + 4) == entries[1]
    assert scripts[entries[1]:entries[1] + len(stock)] == stock


def test_stage_hide_flags_avoid_every_special_flag_range():
    # pret e97c7fc include/constants/flags.h: map-temp, hidden items, trainers,
    # daily (cleared at each day change) and out-of-range flags cannot persist a stage.
    special = [(0x1, 0x40), (800, 800 + 231 + 0x78), (0x550, 0x95F), (0x960, 0xA9F), (0xAA0, 0xB5F), (0xB60, 0xFFFF)]
    assert len(sa.HIDE_FLAGS) == len(set(sa.HIDE_FLAGS)) >= 10
    assert not [f for f in sa.HIDE_FLAGS for lo, hi in special if lo <= f <= hi]


def test_hide_flags_follow_the_replayed_state_not_the_live_document():
    # Redo validates a snapshot while project.doc still holds the undone history.
    flags = sa.HIDE_FLAGS
    state = {'story': {'sequence': {'a': {'hide_flag': flags[0]}, 'b': {'hide_flag': flags[1]}, 'c': {}}}}
    assert sa.allocate_hide_flag(state, None) == flags[2]
    assert sa.allocate_hide_flag(state, {'hide_flag': flags[0]}) == flags[0]
    assert sa.allocate_hide_flag(state, {}) == flags[2]
    full = {'story': {'sequence': {str(i): {'hide_flag': f} for i, f in enumerate(flags)}}}
    with pytest.raises(EditorError, match='capacity'):
        sa.allocate_hide_flag(full, None)


@pytest.mark.parametrize('table', [struct.pack('<BI', 2, 1) * 2 + b'\x00', struct.pack('<BI', 5, 1) + b'\x00',
                                   struct.pack('<BI', 2, 9) + b'\x00', struct.pack('<BI', 2, 1)])
def test_unsupported_init_tables_are_refused(table):
    raw = fmt.append_scripts(b'\x13\xfd', [STOCK_END])
    with pytest.raises(EditorError):
        sa.chain_transition(table, raw, flag_code)
