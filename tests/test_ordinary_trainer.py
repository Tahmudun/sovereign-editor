"""Ordinary authored trainers: stock loss/blackout, persistent defeat, practice preserved."""
import copy
import json
import struct
import sys
from pathlib import Path
import pytest

from sovereign_editor import event_sequences as seq, story_authoring as story, character_runtime as cr
from sovereign_editor.core import Project, atomic_json
from sovereign_editor.formats import EditorError

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT/'projects/scyther-orchestration-1/baseline.nds'
CTX = {'header': 33, 'cell': [18, 12]}
VARS = {'robin_defeated': {'name': 'Robin', 'variable': 0x4164}, 'done': {'name': 'Done', 'variable': 0x4165}}
ORDINARY = {'name': 'Robin', 'character': None, 'stock_class': 3, 'policy': story.ORDINARY, 'defeat_state': 'robin_defeated',
            'party': [{'species': 16, 'level': 4, 'moves': [16, 33, 28, 0], 'held_item': 0},
                      {'species': 19, 'level': 5, 'moves': [98, 33, 39, 0], 'held_item': 155}],
            'before': ['Here I come!'], 'after': ['Good battle!'], 'revisit': ['You already won.'], 'trainer_id': 741}
PRACTICE = {'name': 'Ari', 'character': None, 'stock_class': 2, 'party': [{'species': 19, 'level': 3}],
            'before': ['Practice?'], 'after': ['Thanks!'], 'trainer_id': 739}
NODES = [{'id': 'ask', 'op': 'choice', 'pages': ['Battle?'], 'yes': 'fight', 'no': 'bye'},
         {'id': 'fight', 'op': 'battle', 'trainer': 'robin', 'won': 'done'},
         {'id': 'bye', 'op': 'say', 'pages': ['Later.'], 'next': 'done'}, {'id': 'done', 'op': 'end', 'complete': False}]
SIZES = {2: 0, 17: 4, 22: 4, 28: 5, 41: 4, 45: 1, 49: 0, 53: 0, 63: 2, 73: 2, 96: 0, 97: 0, 104: 0, 213: 6, 219: 0, 220: 2, 282: 0}


def listing(code):
    at, out = 0, []
    while at < len(code):
        op = struct.unpack_from('<H', code, at)[0]; n = SIZES[op]; out.append((op, code[at+2:at+2+n])); at += 2 + n
    return out


def compile_nodes(nodes, trainers):
    spec = {'kind': 'npc', 'nodes': nodes, 'once_state': None}
    return seq.compile_sequence(spec, VARS, trainers, 40)


def test_ordinary_battle_compiles_the_stock_loss_contract():
    ops = listing(compile_nodes(NODES, {'robin': ORDINARY}))
    codes = [op for op, _ in ops]
    assert 282 not in codes                                   # no practice healing
    battle = codes.index(213)
    assert ops[battle][1] == struct.pack('<2H2B', 741, 0, 0, 0)  # stock single battle, no BATTLE_TYPE_11
    # The result is checked before any field access; a loss runs only the stock tail.
    assert codes[battle+1:battle+4] == [220, 17, 28] and ops[battle+2][1] == struct.pack('<2H', 0x800c, 0)
    assert codes[-3:] == [219, 97, 2] and codes.count(219) == 1
    # A win re-locks, persists defeat once, then shows after-battle dialogue.
    assert codes[battle+4:battle+6] == [96, 41] and ops[battle+5][1] == struct.pack('<2H', 0x4164, 1)
    # A defeated trainer never rebattles: the guard precedes the pre-battle pages.
    guard = [i for i, (op, a) in enumerate(ops) if op == 17 and a == struct.pack('<2H', 0x4164, 0)]
    assert guard and guard[0] < battle and ops[guard[0]+1][0] == 28 and ops[guard[0]+1][1][0] == 5
    assert seq.messages(NODES, {'robin': ORDINARY}) == ['Battle?', 'Here I come!', 'Good battle!', 'You already won.', 'Later.']


def test_practice_battle_bytes_keep_historical_flow():
    nodes = [{'id': 'fight', 'op': 'battle', 'trainer': 'ari', 'won': 'done', 'lost': 'done', 'partner': None, 'opponent2': None},
             {'id': 'done', 'op': 'end'}]
    ops = listing(compile_nodes(nodes, {'ari': PRACTICE}))
    codes = [op for op, _ in ops]
    assert codes.count(282) == 2 and 219 not in codes
    battle = codes.index(213)
    assert ops[battle][1] == struct.pack('<2H2B', 739, 0, 1, 0) and codes[battle+1:battle+4] == [96, 220, 282]


@pytest.mark.parametrize('field,value', [('lost', 'done'), ('partner', 'ari'), ('opponent2', 'ari')])
def test_ordinary_battle_refuses_local_loss_or_ally(field, value):
    nodes = copy.deepcopy(NODES); nodes[1][field] = value
    with pytest.raises(EditorError):
        compile_nodes(nodes, {'robin': ORDINARY, 'ari': PRACTICE})


def test_practice_mask_hook_selects_only_practice_trainers():
    sys.path.insert(0, str(ROOT/'work/tiana-fixes-1/python-tools'))
    pytest.importorskip('unicorn')
    from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB
    from unicorn.arm_const import UC_ARM_REG_R4, UC_ARM_REG_R7, UC_ARM_REG_LR, UC_ARM_REG_SP, UC_ARM_REG_R0, UC_ARM_REG_R1
    if not BASELINE.exists():
        pytest.skip('Pinned baseline is local and untracked')
    blob = BASELINE.read_bytes(); package = json.loads((ROOT/'tests/fixtures/tiana.character.json').read_text())
    legacy = cr.bindings(blob, [package], 3)
    assert cr.bindings(blob, [package], 3, practice=[True, True, True]) == legacy
    mixed = cr.bindings(blob, [package], 4, practice=[True, False, True, False])
    info = cr.overlay(blob, 129); ext = mixed['files'][info['file_id']]
    hook_patch = next(p for p in mixed['patches'] if p['kind'] == 'trainer.practice-return')
    u = Uc(UC_ARCH_ARM, UC_MODE_THUMB); u.mem_map(0x02000000, 0x400000); u.mem_write(info['address'], ext)
    lo, hi = struct.unpack('<2H', bytes.fromhex(hook_patch['after']))
    target = 0x020513ac + 4 + ((((lo & 0x7ff) << 12) | ((hi & 0x7ff) << 1)) ^ (1 << 22)) - (1 << 22)
    result = {}
    for tid in (1, 47, 737, 738, 739, 740, 741, 769, 770, 0xffffffff):
        u.reg_write(UC_ARM_REG_R7, tid); u.reg_write(UC_ARM_REG_R4, 1); u.reg_write(UC_ARM_REG_LR, 0x02001001)
        u.reg_write(UC_ARM_REG_SP, 0x02390000); u.emu_start(target | 1, 0x02001000, count=50)
        assert u.reg_read(UC_ARM_REG_R0) == 11 and u.reg_read(UC_ARM_REG_R1) == u.reg_read(UC_ARM_REG_R4)
        result[tid] = u.reg_read(UC_ARM_REG_R4)
    assert {t for t, v in result.items() if v & 0x800} == {738, 740}
    assert all(v & ~0x800 == 1 for v in result.values())
    with pytest.raises(EditorError):
        cr.bindings(blob, [package], 2, practice=[True])
    assert not any(p['kind'] == 'trainer.practice-return' for p in cr.bindings(blob, [package], 2, practice=[False, False])['patches'])


@pytest.fixture(scope='module')
def workspace(tmp_path_factory):
    if not BASELINE.exists():
        pytest.skip('Pinned baseline is local and untracked')
    p = Project.create(BASELINE, tmp_path_factory.mktemp('ordinary')/'project')
    package = json.loads((ROOT/'tests/fixtures/tiana.character.json').read_text())
    ops = [{'kind': 'story', 'context': CTX, 'request': {'kind': 'character', 'key': 'tiana', 'value': package}}]
    for key in ('robin_defeated', 'other_defeated'):
        ops.append({'kind': 'story', 'context': CTX, 'request': {'kind': 'state', 'key': key, 'value': {'name': key}}})
    practice = {k: v for k, v in PRACTICE.items() if k != 'trainer_id'}
    ops.append({'kind': 'story', 'context': CTX, 'request': {'kind': 'trainer', 'key': 'ari', 'value': practice}})
    p.apply_area_edit(0, operations=ops)
    return p.root, copy.deepcopy(p.doc)


@pytest.fixture
def p(workspace):
    root, doc = workspace; atomic_json(root/'project.json', copy.deepcopy(doc))
    return Project(root)


def trainer_op(value):
    return {'kind': 'story', 'context': CTX, 'request': {'kind': 'trainer', 'key': 'robin', 'value': value}}


def ordinary_value():
    return {k: copy.deepcopy(v) for k, v in ORDINARY.items() if k != 'trainer_id'}


@pytest.mark.parametrize('issue', ['policy', 'state', 'rows', 'mixed', 'item', 'move', 'duplicate', 'revisit'])
def test_ordinary_definition_refusals(p, issue):
    v = ordinary_value(); before = p.path.read_bytes()
    if issue == 'policy': v['policy'] = 'ordinary-v9'
    if issue == 'state': v['defeat_state'] = 'unknown'
    if issue == 'rows': del v['party'][0]['moves']
    if issue == 'mixed': v['party'][1]['moves'] = None
    if issue == 'item': v['party'][0]['held_item'] = 2696
    if issue == 'move': v['party'][0]['moves'][0] = 928
    if issue == 'duplicate': v['party'][0]['moves'] = [33, 33, 0, 0]
    if issue == 'revisit': del v['revisit']
    with pytest.raises(EditorError):
        p.plan_area_edit([trainer_op(v)])
    assert p.path.read_bytes() == before


def test_each_ordinary_trainer_owns_its_defeat_state(p):
    other = ordinary_value(); other['name'] = 'Other'
    ops = [trainer_op(ordinary_value()), {'kind': 'story', 'context': CTX, 'request': {'kind': 'trainer', 'key': 'other', 'value': other}}]
    with pytest.raises(EditorError, match='own defeat state'):
        p.plan_area_edit(ops)
    other['defeat_state'] = 'other_defeated'; p.plan_area_edit(ops)


def test_runtime_encodes_chosen_moves_items_and_selects_practice_by_policy(p):
    p.apply_area_edit(p.doc['revision'], operations=[trainer_op(ordinary_value())])
    state = p.composed(); trainers = story.catalog(state, 'trainer')
    assert [t['trainer_id'] for t in trainers.values()] == [738, 739]
    runtime = story.runtime(p, state)
    header, party = runtime['appends'][story.TRAINER_ARCHIVE][1], runtime['appends'][story.PARTY_ARCHIVE][1]
    assert header == struct.pack('<BHB4HIB3x', 3, 3, 2, 0, 0, 0, 0, story.ORDINARY_AI, 0)
    assert party == (struct.pack('<BBHHH4HH', 0, 0, 4, 16, 0, 16, 33, 28, 0, 0) + struct.pack('<BBHHH4HH', 0, 0, 5, 19, 155, 98, 33, 39, 0, 0))
    # Practice trainer 739 keeps its historical header/party layout.
    assert runtime['appends'][story.TRAINER_ARCHIVE][0][:4] == struct.pack('<BHB', 0, 2, 1)
    kinds = [x['kind'] for x in runtime['patches']]
    assert 'trainer.practice-return' in kinds
    reopened = Project(p.root); assert story.catalog(reopened.composed(), 'trainer')['robin']['policy'] == story.ORDINARY
