"""PROD-CAP-001: qualified persistent storage, switch states and capacity limits."""
import struct
import pytest

from sovereign_editor import event_sequences as seq, scene_authoring as scenes, scene_commands as sc, storage
from sovereign_editor.formats import EditorError

SIZES = {2: 0, 17: 4, 22: 4, 28: 5, 30: 2, 31: 2, 32: 2, 41: 4, 45: 1, 49: 0, 53: 0, 63: 2, 73: 2, 96: 0, 97: 0,
         100: 2, 101: 2, 104: 0, 213: 6, 219: 0, 220: 2, 282: 0}


def listing(code):
    at, out = 0, []
    while at < len(code):
        op = struct.unpack_from('<H', code, at)[0]
        n = SIZES[op]
        out.append((op, code[at + 2:at + 2 + n]))
        at += 2 + n
    return out


def test_storage_pools_are_disjoint_sized_and_outside_reset_ranges():
    legacy = set(range(0x4160, 0x4170))
    assert storage.STATE_VARS[:16] == tuple(sorted(legacy))
    assert len(storage.STATE_VARS) == 60 and len(set(storage.STATE_VARS)) == 60
    assert all(0x4020 <= v < 0x4170 for v in storage.STATE_VARS)            # inside vars[NUM_VARS]
    assert not {v for v in storage.STATE_VARS if 0x4000 <= v <= 0x401F}      # map-temp vars
    hide = storage.HIDE_FLAGS
    assert hide[:14] == scenes.HIDE_FLAGS_V1 and len(hide) == 32 and len(set(hide)) == 32
    assert len(storage.STATE_FLAGS) == 98 and not set(storage.STATE_FLAGS) & set(hide)
    for f in (*hide[14:], *storage.STATE_FLAGS):
        assert 0x872 <= f < 0x960                                          # event region
    for f in (*hide, *storage.STATE_FLAGS):
        assert not (1 <= f <= 0x3F or 0x550 <= f <= 0x871 or f >= 0x960)    # map-temp, trainer, system/daily
    assert set(storage.STRICT_FLAGS) <= set(hide[14:]) | set(storage.STATE_FLAGS)
    assert set(hide[14:]) <= set(storage.STRICT_FLAGS)                      # visibility uses strict flags only


NUMBER = {'name': 'Stage', 'variable': 0x4160}
SWITCH = {'name': 'Met', 'switch': True, 'flag': 0x873}


def compiled(nodes, variables, trainers=None, once=None):
    spec = {'kind': 'npc', 'nodes': nodes, 'once_state': once}
    return listing(seq.compile_sequence(spec, variables, trainers or {}, 40))


def test_switch_state_if_and_set_use_flag_commands():
    nodes = [{'id': 'a', 'op': 'if', 'state': 'met', 'value': 1, 'yes': 'b', 'no': 'c'},
             {'id': 'b', 'op': 'set', 'state': 'met', 'value': 0, 'next': 'd'},
             {'id': 'c', 'op': 'set', 'state': 'met', 'value': 1, 'next': 'd'},
             {'id': 'd', 'op': 'end'}]
    ops = compiled(nodes, {'met': SWITCH})
    codes = [op for op, _ in ops]
    assert 17 not in codes and 41 not in codes                               # no variable compare/set
    check = codes.index(32)
    assert ops[check][1] == struct.pack('<H', 0x873) and ops[check + 1][0] == 28 and ops[check + 1][1][0] == 1
    assert (31, struct.pack('<H', 0x873)) in ops and (30, struct.pack('<H', 0x873)) in ops


def test_switch_state_if_zero_branches_when_clear():
    nodes = [{'id': 'a', 'op': 'if', 'state': 'met', 'value': 0, 'yes': 'b', 'no': 'b'}, {'id': 'b', 'op': 'end'}]
    ops = compiled(nodes, {'met': SWITCH})
    check = [op for op, _ in ops].index(32)
    assert ops[check + 1][0] == 28 and ops[check + 1][1][0] == 5            # jump when not set


def test_switch_once_state_guards_and_completes_with_flags():
    ops = compiled([{'id': 'a', 'op': 'end'}], {'met': SWITCH}, once='met')
    assert (32, struct.pack('<H', 0x873)) in ops and (30, struct.pack('<H', 0x873)) in ops
    check = ops.index((32, struct.pack('<H', 0x873)))
    assert ops[check + 1][0] == 28 and ops[check + 1][1][0] == 1            # already done -> exit


def test_number_state_bytes_are_unchanged():
    nodes = [{'id': 'a', 'op': 'set', 'state': 'stage', 'value': 3, 'next': 'b'}, {'id': 'b', 'op': 'end'}]
    ops = compiled(nodes, {'stage': NUMBER})
    assert (41, struct.pack('<2H', 0x4160, 3)) in ops and 30 not in [op for op, _ in ops]


@pytest.mark.parametrize('node', [{'id': 'a', 'op': 'if', 'state': 'met', 'value': 2, 'yes': 'b', 'no': 'b'},
                                  {'id': 'a', 'op': 'set', 'state': 'met', 'value': 7, 'next': 'b'}])
def test_switch_state_refuses_values_other_than_on_off(node):
    with pytest.raises(EditorError, match='on/off'):
        seq.validate([node, {'id': 'b', 'op': 'end'}], {'met': SWITCH}, {})


def test_ordinary_defeat_can_be_a_switch_state():
    trainer = {'name': 'Rei', 'character': None, 'stock_class': 3, 'policy': 'ordinary-single-v1', 'defeat_state': 'beat',
               'party': [{'species': 16, 'level': 4, 'moves': None, 'held_item': 0}],
               'before': ['Go!'], 'after': ['Done.'], 'revisit': ['Again?'], 'trainer_id': 801}
    nodes = [{'id': 'f', 'op': 'battle', 'trainer': 'rei', 'won': 'e'}, {'id': 'e', 'op': 'end'}]
    ops = compiled(nodes, {'beat': {'name': 'Beat', 'switch': True, 'flag': 0x874}}, {'rei': trainer})
    codes = [op for op, _ in ops]
    battle = codes.index(213)
    assert ops[battle][1] == struct.pack('<2H2B', 801, 0, 0, 0)
    guard = codes.index(32)
    assert guard < battle and ops[guard][1] == struct.pack('<H', 0x874) and ops[guard + 1][1][0] == 1
    assert (30, struct.pack('<H', 0x874)) in ops[battle:]                    # defeat persists after a win


def test_presence_on_a_switch_state_uses_checkflag():
    code = sc.Code()
    actors = {'guard': {'presence': {'state': 'met', 'values': [1]}, 'hide_flag': storage.HIDE_FLAGS[20], 'npc_id': 3}}
    sc.emit_visibility(code, actors, {'met': SWITCH})
    code.label('$abort'); code.emit('H', 2)
    ops = listing(code.finish())
    assert (32, struct.pack('<H', 0x873)) in ops and 17 not in [op for op, _ in ops]


def test_presence_condition_on_switch_refuses_other_values():
    with pytest.raises(EditorError, match='on/off'):
        scenes.condition({'state': 'met', 'values': [0, 2]}, {'met': SWITCH})


# ---- Project plans: v3 library capacity, historical v1/v2 rules -----------------------
import copy
import json
from pathlib import Path

from sovereign_editor import story_authoring as story, character_runtime as cr
from sovereign_editor.core import Project

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / 'projects/scyther-orchestration-1/baseline.nds'
CTX = {'header': 33, 'cell': [18, 12]}


@pytest.fixture(scope='module')
def project(tmp_path_factory):
    if not BASELINE.exists():
        pytest.skip('Pinned baseline is local and untracked')
    return Project.create(BASELINE, tmp_path_factory.mktemp('capacity') / 'project')


def state_op(key, switch=None):
    value = {'name': key} if switch is None else {'name': key, 'switch': switch}
    return {'kind': 'story', 'context': CTX, 'request': {'kind': 'state', 'key': key, 'value': value}}


def test_v3_states_take_legacy_slots_then_extra_vars_and_switch_flags(project):
    ops = [state_op(f's{i}') for i in range(17)] + [state_op('on', True), state_op('off', False)]
    plan = project.plan_area_edit(ops)
    after = {t['key']: t['after'] for t in plan['transactions']}
    assert [after[f's{i}']['variable'] for i in range(16)] == list(range(0x4160, 0x4170))
    assert after['s16']['variable'] == storage.EXTRA_STATE_VARS[0]
    assert after['on'] == {'name': 'on', 'switch': True, 'flag': storage.STATE_FLAGS[0]}
    assert after['off']['variable'] == storage.EXTRA_STATE_VARS[1]
    assert {t['schema'] for t in plan['transactions']} == {story.SCHEMAS[2]}


def synthetic(kind, count, extra=None):
    state = {'story': {kind: {f'k{i}': copy.deepcopy(extra or {}) for i in range(count)}}}
    return state


def test_number_and_switch_pools_refuse_when_full(project):
    numbers = {f'n{i}': {'name': 'n', 'variable': v} for i, v in enumerate(storage.STATE_VARS)}
    with pytest.raises(EditorError, match='number state slots'):
        storage.allocate_state(project, numbers, 'more', {'name': 'more'}, None)
    switches = {f'f{i}': {'name': 'f', 'switch': True, 'flag': f} for i, f in enumerate(storage.STATE_FLAGS)}
    assert storage.allocate_state(project, switches, 'n', {'name': 'n'}, None)['variable'] == 0x4160
    with pytest.raises(EditorError, match='on/off state slots'):
        storage.allocate_state(project, switches, 'more', {'name': 'more', 'switch': True}, None)


def test_state_kind_is_stable(project):
    before = {'name': 'x', 'variable': 0x4160}
    with pytest.raises(EditorError, match='kind'):
        storage.allocate_state(project, {'x': before}, 'x', {'name': 'x', 'switch': True}, before)


def test_historical_v2_state_rules_still_refuse_a_seventeenth_slot(project):
    ctx = project.context(**CTX)
    state = synthetic('state', 16, {'name': 'x', 'variable': 0x4160})
    with pytest.raises(EditorError, match='Sixteen'):
        story.plan(project, ctx, state, 0, 'state', 'new', {'name': 'new'}, _version=2)
    with pytest.raises(EditorError):
        story.plan(project, ctx, state, 0, 'state', 'sw', {'name': 'sw', 'switch': True}, _version=2)


def practice(name='Ari'):
    return {'name': name, 'character': None, 'stock_class': 2, 'party': [{'species': 19, 'level': 3}],
            'before': ['Practice?'], 'after': ['Thanks!']}


def test_v3_trainers_reach_sixty_four_ids_then_refuse(project):
    ctx = project.context(**CTX)
    state = synthetic('trainer', 63)
    state['story']['character'] = {'c': {}}
    t = story.plan(project, ctx, state, 0, 'trainer', 'last', practice())
    assert t['after']['trainer_id'] == 738 + 63 and t['schema'] == story.SCHEMAS[2]
    state = synthetic('trainer', 64)
    with pytest.raises(EditorError, match='64'):
        story.plan(project, ctx, state, 0, 'trainer', 'over', practice())
    with pytest.raises(EditorError, match='32'):
        story.plan(project, ctx, synthetic('trainer', 32), 0, 'trainer', 'old', practice(), _version=2)


def test_v3_characters_reach_thirty_two_then_refuse(project):
    ctx = project.context(**CTX)
    package = json.loads((ROOT / 'tests/fixtures/tiana.character.json').read_text())
    t = story.plan(project, ctx, synthetic('character', 31), 0, 'character', 'last', package)
    assert t['schema'] == story.SCHEMAS[2]
    with pytest.raises(EditorError, match='32'):
        story.plan(project, ctx, synthetic('character', 32), 0, 'character', 'over', package)
    with pytest.raises(EditorError, match='eight'):
        story.plan(project, ctx, synthetic('character', 8), 0, 'character', 'old', package, _version=2)


def sequence(kind, **extra):
    value = {'kind': kind, 'x': 590, 'z': 400, 'donor_id': None, 'facing': 0, 'movement': 0, 'range_x': 0,
             'range_z': 0, 'character': None, 'nodes': [{'id': 'a', 'op': 'end'}], 'once_state': None}
    value.update(extra)
    return value


def switch_state():
    return {'story': {'state': {'met': {'name': 'Met', 'switch': True, 'flag': storage.STATE_FLAGS[0]},
                                'stage': {'name': 'Stage', 'variable': 0x4160}}}}


def test_step_on_trigger_refuses_a_switch_once_state(project):
    ctx = project.context(**CTX)
    with pytest.raises(EditorError, match='number state'):
        story.plan(project, ctx, switch_state(), 0, 'sequence', 'step', sequence('trigger', once_state='met'))


@pytest.mark.parametrize('kind,trigger', [('entry', {'state': 'met', 'value': 0, 'advance': 1}),
                                          ('trigger', {'state': 'met', 'value': 1, 'width': 1, 'height': 1})])
def test_stage_conditions_refuse_switch_states(project, kind, trigger):
    ctx = project.context(**CTX)
    with pytest.raises(EditorError, match='number state'):
        scenes.extend_plan(project, ctx, switch_state(), 0, sequence(kind, trigger=trigger), {}, None, 2)


def test_shared_capacity_report_counts_every_pool(project):
    project.apply_area_edit(project.doc['revision'], operations=[state_op('alpha'), state_op('flip', True)])
    report = project.capacity()
    limits = report['limits']
    pick = lambda d: {k: d[k] for k in ('used', 'limit', 'available')}
    assert pick(limits['number_states']) == {'used': 1, 'limit': 60, 'available': 59}
    assert pick(limits['switch_states']) == {'used': 1, 'limit': 98, 'available': 97}
    assert limits['named_states']['limit'] == 158 and limits['characters']['limit'] == 32
    assert limits['trainers']['limit'] == 64 and limits['created_headers']['limit'] == 152
    assert limits['visibility_actors']['limit'] == 32 and limits['area_cells']['limit'] == 16
    from sovereign_editor.formats import member_count
    assert limits['private_encounter_tables']['available'] == 255 - member_count(project.blob, 'a/0/3/7')
    assert report['resident']['layout'] == 1 and report['resident']['limit'] == 32768
    assert all('refusal' in v for v in limits.values())


def test_cli_capacity_and_runtime_report_share_the_project_operation(project, capsys):
    from sovereign_editor import cli
    assert cli.main(['capacity', '--project', str(project.root)]) == 0
    out = json.loads(capsys.readouterr().out)['result']
    assert out['limits']['number_states']['used'] >= 1
    assert cli.main(['runtime-report', '--project', str(project.root)]) == 0
    out = json.loads(capsys.readouterr().out)['result']
    assert out['overlay_129']['layout'] == 1 and out['overlay_129']['limit'] == 32768


def test_exact_arm9_accessors_and_clears_preserve_every_allocation():
    """Real Save_VarsFlags accessors, map-transition and day-change clears (CPU)."""
    if not BASELINE.exists():
        pytest.skip('Pinned baseline is local and untracked')
    import sys
    sys.path.insert(0, str(ROOT / 'work/tiana-fixes-1/python-tools'))
    sys.path.insert(0, str(ROOT / 'tools'))
    pytest.importorskip('unicorn')
    import storage_qualification as sq
    result = sq.cpu(BASELINE.read_bytes())
    assert result['failed'] == 0
    assert result['vars_checked'] == 60 and result['flags_checked'] == 32 + 98
    assert result['asserts'] == 0
