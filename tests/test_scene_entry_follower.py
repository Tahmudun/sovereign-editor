"""Scene support repaired after r28 native feedback (evidence/scyther-event-repair-1).

Movement-data alignment (the Cherrygrove freeze), follower-safe player travel,
entry scenes from the native frame table and follower-aware route qualification.
Software checks against pinned pret e97c7fc behaviour, not melonDS acceptance.
"""
import struct

import pytest

from sovereign_editor import dialogue_format as fmt, event_sequences as seq, scene_authoring as sa
from sovereign_editor.formats import EditorError
from tests.test_scene_safety import setup, move, finish
from tests.test_scyther_quest import QuestVM

STAGE = 0x4160
VARIABLES = {'stage': {'variable': STAGE}}


def ldrh(raw, at):
    # ARM946E-S (and melonDS) ignore bit 0 of a halfword load address.
    return struct.unpack_from('<H', raw, at & ~1)[0]


def movement_targets(raw, start):
    """(actor, target) for each ApplyMovement ... WaitMovement the compiler emitted."""
    found = []
    for p in range(start, len(raw) - 10):
        if raw[p:p+2] == b'\x5e\x00' and raw[p+8:p+10] == b'\x5f\x00':
            actor, delta = struct.unpack_from('<Hi', raw, p + 2)
            found.append((actor, p + 8 + delta))
    return found


def native_commands(raw, target, limit=70):
    """What pret MovementScriptMachine reads: LDRH command/length until EndMovement."""
    result = []
    for _ in range(limit):
        if (target & ~1) + 4 > len(raw): return None       # ran off the script: never ends
        cmd = ldrh(raw, target)
        if cmd == 254: return result
        result.append((cmd, ldrh(raw, target + 2))); target += 4
    return None


def walk_spec():
    return dict(kind='trigger', nodes=[move('walk', [[2, 2], [4, 2]], 'end'), dict(id='end', op='end')])


def compile_walk():
    return seq.compile_sequence(walk_spec(), {}, {}, 1, {'actors': {'a': {'npc_id': 1}}, 'variables': {}})


def test_scene_movement_is_4_aligned_after_odd_length_scripts():
    # r28 appended scene scripts at whatever offset the file ended: Cherrygrove's
    # beach/town movement data sat at offset%4 == 3, so LDRH read shifted bytes.
    code, odd, even = compile_walk(), b'\x02\x00\x02', b'\x02\x00'
    base = fmt.append_scripts(b'\x13\xfd', [odd])
    unaligned = fmt.append_scripts(base, [even, code])
    start = fmt.script_entries(unaligned)[1][-1]
    assert start % 2 == 1                                  # the r28 layout
    (_, target), = movement_targets(unaligned, start)
    # Step right x2 reads as command 0x0Fxx and EndMovement as 0xFE00: no terminator.
    assert target % 2 == 1 and ldrh(unaligned, target) >> 8 == 15 and ldrh(unaligned, target + 4) == 0xFE00
    assert native_commands(unaligned, target) is None
    aligned = fmt.append_scripts(base, [even, code], [1, seq.alignment(walk_spec())])
    start = fmt.script_entries(aligned)[1][-1]
    (_, target), = movement_targets(aligned, start)
    assert start % 4 == 0 and target % 4 == 0 and native_commands(aligned, target) == [(15, 2)]
    assert aligned[start:start + len(code)] == code
    # Default alignment keeps historical output byte-identical.
    assert fmt.append_scripts(base, [even, code], [1, 1]) == unaligned
    assert seq.alignment({'nodes': [dict(id='end', op='end')]}) == 1


def gather_code(route, extra=()):
    spec = dict(kind='trigger', trigger={'state': 'stage', 'value': 0, 'width': 1, 'height': 1},
                nodes=[dict(id='gather', op='gather', destination=list(route[-1]), next='advance'), *extra,
                       *finish()])
    return seq.compile_sequence(spec, VARIABLES, {}, 1, {'actors': {'a': {'npc_id': 1}}, 'variables': VARIABLES,
                                                        'routes': {'gather': {route[0]: list(route)}}})


def test_player_travel_keeps_the_follower_following():
    code = gather_code([(0, 0), (0, 1), (1, 1)])
    at = code.index(struct.pack('<2H', 94, 255))
    assert code[at-10:at] == struct.pack('<5H', 602, 0, 603, 604, 55)       # unpause, wait, follow
    assert code[at+8:at+20] == struct.pack('<H5H', 95, 603, 602, 1, 604, 48)  # wait, pause, restore
    vm = QuestVM(); vm.positions = {255: (0, 0)}; vm.execute(code)
    assert vm.positions[255] == (1, 1) and vm.values[STAGE] == 1 and not vm.follower_free


def test_npc_movement_leaves_the_follower_paused():
    code = compile_walk()
    assert struct.pack('<H', 602) not in code[:code.index(struct.pack('<2H', 94, 1))]
    vm = QuestVM(); vm.positions = {1: (2, 2)}; vm.execute(code)
    assert vm.positions[1] == (4, 2)


# --- frame table -------------------------------------------------------------

def load_script_id(init, kind):
    """pret GetMapLoadScriptId."""
    at = 0
    while init[at]:
        if init[at] == kind: return init[at+1] + (init[at+2] << 8)
        at += 5
    return 0xFFFF


def scene_script_id(init, variables):
    """pret GetMapSceneScriptId with FieldSystem_VarGet (IDs below 0x4000 are literals)."""
    at = 0
    while True:
        if init[at] == 0: return 0xFFFF
        if init[at] == 1:
            ofs = struct.unpack_from('<I', init, at + 1)[0]; at += 5; break
        at += 5
    if ofs == 0: return 0xFFFF
    at += ofs
    get = lambda v: variables.get(v, 0) if v >= 0x4000 else v
    while True:
        var = struct.unpack_from('<H', init, at)[0]
        if var == 0: return 0xFFFF
        value, script = struct.unpack_from('<2H', init, at + 2)
        if get(var) == get(value): return script
        at += 6


def test_entry_row_is_added_to_an_empty_init_table():
    empty = b'\x00\x00\x00\x00'                       # native InitScriptEntryEnd + .balign 4
    init = sa.add_frame_rows(empty, [(STAGE, 0, 7)])
    assert init[5:9] == empty and len(init) % 4 == 0
    assert scene_script_id(init, {}) == 7 and scene_script_id(init, {STAGE: 1}) == 0xFFFF


def test_stock_frame_rows_stay_first_and_bytes_are_kept():
    stock_table = struct.pack('<3H', 0x4001, 1, 3) + b'\0\0'
    init = struct.pack('<BI', 1, 6) + struct.pack('<BHH', 2, 2, 0) + b'\x00' + stock_table   # table after the end byte
    init += b'\0' * (-len(init) % 4)
    patched = sa.add_frame_rows(init, [(STAGE, 0, 9)])
    assert patched[5:len(init)] == init[5:] and patched[0] == 1           # only the table offset moved
    assert scene_script_id(patched, {0x4001: 1}) == 3                      # stock row keeps priority
    assert scene_script_id(patched, {}) == 9 and scene_script_id(patched, {STAGE: 1}) == 0xFFFF
    assert scene_script_id(init, {0x4001: 1}) == 3 and load_script_id(patched, 2) == 2


def test_entry_rows_and_stage_visibility_share_one_init_table():
    raw = fmt.append_scripts(b'\x13\xfd', [b'\x02\x00'])
    init, scripts = sa.chain_transition(b'\x00\x00\x00\x00', raw, lambda c: c.emit('2H', 30, 0x521))
    init = sa.add_frame_rows(init, [(STAGE, 0, 5)])
    assert load_script_id(init, 2) == 2 and scene_script_id(init, {}) == 5


@pytest.mark.parametrize('rows', [[(0x4000, 0x4000, 5)], [(0x3fff, 0, 5)], [(STAGE, 0, 0)], []])
def test_invalid_entry_rows_are_refused(rows):
    with pytest.raises(EditorError):
        sa.add_frame_rows(b'\x00\x00\x00\x00', rows)


# --- entry scene bytecode --------------------------------------------------

def entry_spec():
    return dict(kind='entry', x=4, z=8, trigger={'state': 'stage', 'value': 0, 'advance': 1},
                nodes=[dict(id='gather', op='gather', destination=[4, 7], next='leave'),
                       move('leave', [[2, 6], [2, 7]], 'advance', actor='tiana'),
                       dict(id='advance', op='set', state='stage', value=1, next='sync'),
                       dict(id='sync', op='sync', next='end'), dict(id='end', op='end')])


def run_entry(player, tiana=(2, 6), stage=0):
    actors = {'tiana': {'npc_id': 1, 'hide_flag': 0x521, 'x': 2, 'z': 6,
                        'presence': {'state': 'stage', 'values': [0]}}}
    code = seq.compile_sequence(entry_spec(), VARIABLES, {}, 1, {'actors': actors, 'variables': VARIABLES,
                                                                 'routes': {'gather': {(4, 8): [(4, 8), (4, 7)]}}})
    vm = QuestVM(); vm.values = {STAGE: stage}; vm.alive = {1}; vm.positions = {255: player, 1: tiana}
    vm.actors = lambda: actors
    vm.execute(code)
    return vm


def test_entry_scene_runs_once_from_the_arrival_tile():
    vm = run_entry((4, 8))
    assert vm.values[STAGE] == 1 and vm.positions[255] == (4, 7) and 0x521 in vm.flags and 1 not in vm.alive


@pytest.mark.parametrize('player,tiana,settled', [((6, 3), (2, 6), (6, 3)), ((4, 8), (5, 5), (4, 7))])
def test_entry_scene_abort_still_advances_so_the_frame_table_stops(player, tiana, settled):
    # A save loaded mid-room (no gather), or a stale actor after the gather:
    # no further movement, actors only hidden, the stage still advances.
    vm = run_entry(player, tiana)
    assert vm.values[STAGE] == 1 and vm.positions[255] == settled and 1 not in vm.alive and not vm.locked


def test_entry_scene_is_quiet_after_its_stage():
    vm = run_entry((4, 8), stage=1)
    assert vm.values[STAGE] == 1 and 96 not in vm.log and vm.positions[255] == (4, 8)


# --- follower-aware qualification -------------------------------------------

def gather_scene(monkeypatch, tail, destination=(0, 2), actors=None):
    return setup(monkeypatch, [dict(id='gather', op='gather', destination=list(destination), next='go'), *tail],
                 actors=actors)


def test_npc_route_through_the_trailing_follower_is_refused(monkeypatch):
    # Player gathers (0,0)->(0,1)->(0,2), so the follower ends on (0,1).
    args = gather_scene(monkeypatch, [move('go', [[2, 2], [2, 1], [0, 1]]), *finish()])
    sa.validate_routes(*args)                               # v1 history keeps its rules
    with pytest.raises(EditorError, match='following Pokémon'):
        sa.validate_routes(*args, version=2)
    args[2]['nodes'][1]['path'] = [[2, 2], [2, 1], [1, 1]]
    sa.validate_routes(*args, version=2)


def test_follower_is_unknown_neighbour_when_the_player_did_not_move(monkeypatch):
    args = gather_scene(monkeypatch, [move('go', [[2, 2], [2, 0], [1, 0]]), *finish()], destination=(0, 0))
    with pytest.raises(EditorError, match='following Pokémon'):
        sa.validate_routes(*args, version=2)


def test_shown_actor_on_the_follower_tile_is_refused(monkeypatch):
    actors = {'a': dict(kind='npc', x=2, z=2, npc_id=1, event_member=1),
              'b': dict(kind='npc', x=0, z=1, npc_id=2, event_member=1, presence={'state': 'stage', 'values': [1]})}
    tail = [dict(id='go', op='set', state='stage', value=1, next='sync'), dict(id='sync', op='sync', next='end'),
            dict(id='end', op='end')]
    args = gather_scene(monkeypatch, tail, actors=actors)
    with pytest.raises(EditorError, match='following Pokémon'):
        sa.validate_routes(*args, version=2)


def entry_scene(monkeypatch, tail, gather=None):
    rows = [dict(kind='warp', id=0, x=0, z=0)]
    nodes = [gather or dict(id='gather', op='gather', destination=[1, 0], next='go'), *tail]
    project, state, s = setup(monkeypatch, nodes, rows=rows)
    s.update(kind='entry', trigger={'state': 'stage', 'value': 0, 'advance': 1})
    return project, state, s


def advance():
    return [dict(id='advance', op='set', state='stage', value=1, next='end'), dict(id='end', op='end')]


def test_entry_scene_moves_only_after_gather_confirms_the_arrival(monkeypatch):
    sa.validate_routes(*entry_scene(monkeypatch, [move('go', [[2, 2], [2, 1]]), *advance()]), version=2)
    args = entry_scene(monkeypatch, [move('go', [[2, 2], [2, 1]]), *advance()],
                       gather=dict(id='gather', op='set', state='stage', value=0, next='go'))
    with pytest.raises(EditorError, match='gather first'):
        sa.validate_routes(*args, version=2)


def test_entry_fallback_branch_cannot_move_or_show_actors(monkeypatch):
    fallback = dict(id='gather', op='gather', destination=[1, 0], next='go', no='advance')
    sa.validate_routes(*entry_scene(monkeypatch, [move('go', [[2, 2], [2, 1]]), *advance()], fallback), version=2)
    args = entry_scene(monkeypatch, [dict(id='go', op='say', pages=['Hi'], next='advance'),
                                     move('advance', [[2, 2], [2, 1]], 'set'),
                                     dict(id='set', op='set', state='stage', value=1, next='end'),
                                     dict(id='end', op='end')],
                       dict(id='gather', op='gather', destination=[1, 0], next='go', no='advance'))
    with pytest.raises(EditorError, match='gather first'):
        sa.validate_routes(*args, version=2)


def test_entry_scene_must_end_at_its_advance_stage(monkeypatch):
    args = entry_scene(monkeypatch, [dict(id='go', op='set', state='stage', value=2, next='end'), dict(id='end', op='end')])
    with pytest.raises(EditorError, match='advance stage'):
        sa.validate_routes(*args, version=2)


def test_dialogue_only_entry_scene_must_still_advance(monkeypatch):
    # No movement ops, but a frame-table scene that never advances reruns forever.
    args = entry_scene(monkeypatch, [], gather=dict(id='gather', op='say', pages=['Hi'], next='end'))
    args[2]['nodes'].append(dict(id='end', op='end'))
    with pytest.raises(EditorError, match='advance'):
        sa.validate_routes(*args, version=2)
    args[2]['nodes'][0]['next'] = 'advance'; args[2]['nodes'][1:] = advance()
    sa.validate_routes(*args, version=2)


def test_unchanged_put_brings_an_old_scene_under_current_rules_once():
    from sovereign_editor import story_authoring as story
    t = {'kind': 'sequence', 'key': 'a', 'before': {'x': 1}, 'after': {'x': 1}, 'schema': story.SCENE_SCHEMA}
    assert story.changed({'scene_versions': {'a': 1}}, t)          # r28-era record: upgrade
    assert not story.changed({'scene_versions': {'a': 2}}, t)      # already v2: no-op
    assert not story.changed({}, {**t, 'kind': 'state'})
    assert story.changed({'scene_versions': {'a': 2}}, {**t, 'after': None})
