"""Editor v1 completion candidate (docs/EDITOR_COMPLETION_V1_*). Software checks only."""
import contextlib
import io
import json
import struct
from pathlib import Path

import pytest

from sovereign_editor import cli, event_sequences as seq, story_authoring as sa, dialogue_format as fmt, travel
from sovereign_editor.core import Project
from sovereign_editor.formats import EditorError

ROOT = Path(__file__).resolve().parents[1]
# The returned r64 parent (read-only here; every test that writes works on a clone).
PARENT = ROOT / 'projects/assets-gameplay-v1'
pytestmark = pytest.mark.skipif(not PARENT.is_dir(), reason='returned assets-gameplay-v1 (r64) parent absent')

WARP_TAIL = struct.pack('<5HH', 174, 6, 1, 1, 0, 175)


def fresh(tmp_path, name='p'):
    clone = Project(PARENT).clone(tmp_path / name)
    assert (clone.doc['revision'], len(clone.doc['map_edits'])) == (64, 567)
    return clone


def run_cli(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cli.main([str(a) for a in argv])
    return json.loads(out.getvalue())


def guide(key, x, z, donor, dest):
    return {'kind': 'sequence', 'key': key, 'action': 'put', 'value': {
        'kind': 'npc', 'x': x, 'z': z, 'donor_id': donor, 'facing': 1, 'movement': 0, 'range_x': 0, 'range_z': 0,
        'character': None, 'stock_sprite': 333, 'once_state': None, 'nodes': [
            {'id': 'offer', 'op': 'choice', 'pages': ['Travel?'], 'yes': 'go', 'no': 'done'},
            {'id': 'go', 'op': 'warp', **dest},
            {'id': 'done', 'op': 'end', 'complete': False}]}}


ACCESS = [{'kind': 'story', 'context': {'header': 33, 'cell': [19, 12]},
           'request': guide('access_r29_guide', 630, 409, 0, {'header': 67, 'x': 570, 'z': 399, 'facing': 0})},
          {'kind': 'story', 'context': {'header': 67, 'cell': [17, 12]},
           'request': guide('access_cherry_guide', 570, 398, 1, {'header': 33, 'x': 630, 'z': 410, 'facing': 0})}]


def test_clone_cli_copies_history_assets_and_inspects(tmp_path):
    out = tmp_path / 'successor'
    result = run_cli('clone', '--project', PARENT, '--output', out, '--name', 'Successor')
    assert result['ok'] and result['result']['revision'] == 64 and result['result']['name'] == 'Successor'
    parent, child = json.loads((PARENT / 'project.json').read_text()), json.loads((out / 'project.json').read_text())
    assert child == {**parent, 'name': 'Successor'}
    assert sorted(p.relative_to(out) for p in (out / 'assets').rglob('*')) == \
        sorted(p.relative_to(PARENT) for p in (PARENT / 'assets').rglob('*'))
    # Prop register/revise transactions are project-wide (no map context); inspect lists map contexts only.
    assert any(t.get('context') is None for t in child['map_edits'])
    assert 'h67/m0/c17,12' in result['result']['map_authoring']['contexts']
    again = run_cli('clone', '--project', PARENT, '--output', out)
    assert not again['ok'] and again['error']['code'] == 'EXISTS'


def test_warp_step_shape_and_stock_pattern():
    nodes = [{'id': 'go', 'op': 'warp', 'header': 67, 'x': 570, 'z': 399, 'facing': 0}]
    seq.validate(nodes, {}, {})
    for bad in ({'facing': 4}, {'x': -1}, {'header': None}, {'next': 'go'}):
        with pytest.raises(EditorError):
            seq.validate([{**nodes[0], **bad}], {}, {})
    code = seq.compile_sequence({'kind': 'npc', 'nodes': nodes}, {}, {}, 0)
    head = struct.pack('<5HH6H', 174, 6, 1, 0, 0, 175, 176, 67, 0, 570, 399, 0)
    at = code.index(head)
    assert code[at + len(head):at + len(head) + len(WARP_TAIL)] == WARP_TAIL
    # The warp ends the event: release all, then end.
    assert code.endswith(struct.pack('<2H', 97, 2))


def test_access_guides_qualify_protect_destination_and_undo(tmp_path):
    p = fresh(tmp_path)
    before = p.path.read_bytes()
    # The destination must be clear, including the follower tile behind the player.
    for dest, reason in (({'header': 67, 'x': 555, 'z': 399, 'facing': 0}, 'NPC'),        # NPC 1 (moved by r64)
                         ({'header': 67, 'x': 564, 'z': 392, 'facing': 1}, 'warp'),       # follower on the PC door
                         ({'header': 67, 'x': 700, 'z': 399, 'facing': 0}, 'outside')):
        op = {**ACCESS[0], 'request': guide('bad_guide', 630, 409, 0, dest)}
        with pytest.raises(EditorError) as e:
            p.plan_area_edit([op])
        assert e.value.code == 'INVALID_DESTINATION' and reason in str(e.value)
    result = p.apply_area_edit(64, operations=ACCESS, label='access')
    assert result['revision'] == 65
    state = p.composed()
    replaced = sa.replacements(p, state)[fmt.SCRIPT_ARCHIVE]
    for key, dest in (('access_r29_guide', (67, 570, 399, 0)), ('access_cherry_guide', (33, 630, 410, 0))):
        member = sa.catalog(state, 'sequence')[key]['script_member']
        assert replaced[member].count(struct.pack('<5HH6H', 174, 6, 1, 0, 0, 175, 176, dest[0], 0, *dest[1:]) + WARP_TAIL) == 1
    # A later edit that occupies a guide's arrival tile is refused by whole-project validation.
    blocker = {'kind': 'story', 'context': {'header': 67, 'cell': [17, 12]},
               'request': guide('blocker', 570, 399, 1, {'header': 33, 'x': 630, 'z': 410, 'facing': 0})}
    with pytest.raises(EditorError) as e:
        p.plan_area_edit([blocker])
    assert e.value.code == 'INVALID_DESTINATION' and 'access_r29_guide' in str(e.value)
    p.undo(65)
    assert p.doc['revision'] == 66 and 'access_r29_guide' not in sa.catalog(p.composed(), 'sequence')
    p.redo(66)
    assert 'access_cherry_guide' in sa.catalog(p.composed(), 'sequence')
    assert p.path.read_bytes() != before


# ---- field obstacles (FIELD-01/02/04) ------------------------------------------------------

from sovereign_editor import script_disasm as sd, field_moves as fm, event_authoring as ev  # noqa: E402

R29 = {'header': 33, 'cell': [19, 12]}


def switch_state(key, name):
    return {'kind': 'story', 'context': R29,
            'request': {'kind': 'state', 'key': key, 'action': 'put', 'value': {'name': name, 'switch': True}}}


def obstacle(key, x, z, family, persistence, authority, state=None, nodes=None):
    field = {'family': family, 'persistence': persistence, **({'state': state} if state else {})}
    nodes = nodes or [
        {'id': 'auth', 'op': 'require', **authority, 'yes': 'ask', 'no': 'blocked'},
        {'id': 'blocked', 'op': 'say', 'pages': ['You need a permit.'], 'next': 'done'},
        {'id': 'ask', 'op': 'choice', 'pages': ['Use the move?'], 'yes': 'act', 'no': 'done'},
        {'id': 'act', 'op': 'field_move', 'move': family, 'yes': 'done', 'no': 'nomon'},
        {'id': 'nomon', 'op': 'say', 'pages': ['No Pokémon knows it.'], 'next': 'done'},
        {'id': 'done', 'op': 'end', 'complete': False}]
    return {'kind': 'story', 'context': R29, 'request': {'kind': 'sequence', 'key': key, 'action': 'put', 'value': {
        'kind': 'npc', 'x': x, 'z': z, 'donor_id': 0, 'facing': 1, 'movement': 0, 'range_x': 0, 'range_z': 0,
        'character': None, 'once_state': None, 'field': field, 'nodes': nodes}}}


FIELD_OPS = [switch_state('ec_permit', 'Trail permit'), switch_state('ec_rock_cleared', 'Rock cleared'),
             obstacle('ec_cut', 634, 410, 'cut', 'reset', {'kind': 'state', 'state': 'ec_permit', 'value': 1}),
             obstacle('ec_rock', 636, 410, 'rock_smash', 'permanent', {'kind': 'badge', 'badge': 0}, 'ec_rock_cleared'),
             obstacle('ec_boulder', 628, 408, 'strength', 'reset', {'kind': 'item', 'item': 1, 'count': 1})]


def names(listing):
    return [(name, tuple(args)) for _, (op, name, args, _) in sorted(listing.items())]


def test_field_obstacles_mirror_the_stock_action_and_own_their_flags(tmp_path):
    p = fresh(tmp_path)
    p.apply_area_edit(64, operations=FIELD_OPS, label='field')
    state = p.composed()
    seqs = sa.catalog(state, 'sequence')
    cut, rock, boulder = seqs['ec_cut'], seqs['ec_rock'], seqs['ec_boulder']
    stock_flags = {r['flag'] for r in ev.records(ev.base(p, cut['event_member'])) if r['kind'] == 'npc'}
    assert cut['field_flag'] in fm.MAP_TEMP_FLAGS and cut['field_flag'] not in stock_flags
    assert rock['field_flag'] == sa.catalog(state, 'state')['ec_rock_cleared']['flag'] and boulder['field_flag'] == 0
    # The object records carry the stock family appearance and the owned flag.
    records = {r['id']: r for r in ev.records(ev.raw_member(p, cut['event_member'], state)) if r['kind'] == 'npc'}
    for spec, family in ((cut, 'cut'), (rock, 'rock_smash'), (boulder, 'strength')):
        r = records[spec['npc_id']]
        assert (r['sprite'], r['flag'], r['x'], r['z']) == (fm.FAMILIES[family]['sprite'], spec['field_flag'], spec['x'], spec['z'])
    # Native action: the stock Cut script's action commands, in order, then our flag and hide.
    raw = sa.replacements(p, state)[fmt.SCRIPT_ARCHIVE][cut['script_member']]
    ids = sa.allocation(p, state)
    ours = names(sd.disassemble(raw, [sd.entries(raw)[ids['ec_cut'][0] - 1]]))
    stock_raw = p.resource(fmt.SCRIPT_ARCHIVE, 146)[1]
    stock = names(sd.disassemble(stock_raw, [sd.entries(stock_raw)[0]]))
    keep = {'get_party_slot_with_move', 'copyvar', 'scrcmd_727', 'scrcmd_730', 'get_player_state', 'scrcmd_183',
            'scrcmd_560', 'scrcmd_598', 'get_party_lead_alive', 'get_partymon_species', 'scrcmd_732', 'scrcmd_733',
            'scrcmd_734', 'play_cry', 'wait_cry', 'wait', 'hide_person', 'check_badge'}
    assert {c for c in ours if c[0] in keep and c[0] != 'check_badge'} == {c for c in stock if c[0] in keep and c[0] != 'check_badge'}
    assert ('setflag', (cut['field_flag'],)) in ours and ('hide_person', (0x800D,)) in ours
    assert ('checkflag', (sa.catalog(state, 'state')['ec_permit']['flag'],)) in ours
    rock_ops = names(sd.disassemble(raw, [sd.entries(raw)[ids['ec_rock'][0] - 1]]))
    assert ('check_badge', (0, 0x800C)) in rock_ops and ('setflag', (rock['field_flag'],)) in rock_ops
    boulder_ops = names(sd.disassemble(raw, [sd.entries(raw)[ids['ec_boulder'][0] - 1]]))
    assert ('strength_flag_action', (1,)) in boulder_ops and ('hasitem', (1, 1, 0x800C)) in boulder_ops
    assert not any(c[0] in ('setflag', 'hide_person') for c in boulder_ops)


def test_field_obstacle_refusals(tmp_path):
    p = fresh(tmp_path)
    base = [switch_state('ec_permit', 'Trail permit')]
    auth = {'kind': 'state', 'state': 'ec_permit', 'value': 1}
    wrong_family = obstacle('bad', 634, 410, 'cut', 'reset', auth)
    wrong_family['request']['value']['nodes'][3]['move'] = 'rock_smash'
    plain = obstacle('bad', 634, 410, 'cut', 'reset', auth)
    del plain['request']['value']['field']
    plain['request']['value']['stock_sprite'] = 333
    for ops, code in ((base + [wrong_family], 'INVALID_EVENT'),
                      (base + [plain], 'INVALID_EVENT'),                                   # field move outside an obstacle
                      (base + [obstacle('bad', 628, 408, 'strength', 'permanent', auth, 'ec_permit')], 'INVALID_INPUT'),
                      (base + [obstacle('bad', 634, 410, 'cut', 'permanent', auth, 'room_welcome')], 'INVALID_INPUT'),  # number state
                      (base + [obstacle('a', 634, 410, 'cut', 'permanent', auth, 'ec_permit'),
                               obstacle('b', 636, 410, 'cut', 'permanent', auth, 'ec_permit')], 'STATE_CONFLICT'),
                      (base + [obstacle('bad', 638, 409, 'cut', 'reset', auth)], 'BLOCKED_TILE')):
        with pytest.raises(EditorError) as e:
            p.plan_area_edit(ops)
        assert e.value.code == code, (code, str(e.value))


def test_requirement_and_badge_steps_compile_native_commands():
    variables = {'permit': {'name': 'Permit', 'switch': True, 'flag': 0x925}}
    nodes = [{'id': 'm', 'op': 'require', 'kind': 'move', 'move': 57, 'yes': 'b', 'no': 'e'},
             {'id': 'b', 'op': 'require', 'kind': 'badge', 'badge': 3, 'yes': 'i', 'no': 'e'},
             {'id': 'i', 'op': 'require', 'kind': 'item', 'item': 450, 'count': 1, 'yes': 'c', 'no': 'e'},
             {'id': 'c', 'op': 'require', 'kind': 'money', 'amount': 500, 'yes': 's', 'no': 'e'},
             {'id': 's', 'op': 'require', 'kind': 'state', 'state': 'permit', 'value': 1, 'yes': 'g', 'no': 'e'},
             {'id': 'g', 'op': 'give_badge', 'badge': 3, 'next': 'e'},
             {'id': 'e', 'op': 'end', 'complete': False}]
    code = seq.compile_sequence({'kind': 'npc', 'nodes': nodes}, variables, {}, 0)
    ops = names(sd.disassemble(code + b'', [0]))
    for expected in (('get_party_slot_with_move', (0x800C, 57)), ('check_badge', (3, 0x800C)),
                     ('hasitem', (450, 1, 0x800C)), ('hasenoughmoneyimmediate', (0x800C, 500)),
                     ('checkflag', (0x925,)), ('give_badge', (3,))):
        assert expected in ops
    for bad in ({'kind': 'badge', 'badge': 16}, {'kind': 'state', 'state': 'nope', 'value': 1}, {'kind': 'weather'}):
        with pytest.raises(EditorError):
            seq.validate([{'id': 'x', 'op': 'require', **bad, 'yes': 'x', 'no': 'x'}], variables, {})


# ---- presets (FIELD-05, WORKSPACE-03) --------------------------------------------------------

def preset(request):
    return {'kind': 'preset', 'context': R29, 'request': request}


PRESETS = [
    preset({'preset': 'gate', 'key': 'ec_gate', 'x': 612, 'z': 410, 'donor_id': 0, 'look': 'rock',
            'state': {'new': 'ec_gate_open', 'name': 'Gate open'},
            'switch': {'key': 'ec_gate_switch', 'x': 614, 'z': 409, 'donor_id': 0, 'appearance': 333,
                       'authority': {'kind': 'money', 'amount': 100}}}),
    preset({'preset': 'pickup', 'key': 'ec_potion', 'x': 608, 'z': 409, 'donor_id': 0, 'item': 17, 'count': 2,
            'state': {'new': 'ec_potion_taken', 'name': 'Potion taken'}}),
    preset({'preset': 'pickup', 'key': 'ec_berry', 'x': 610, 'z': 409, 'donor_id': 0, 'item': 17, 'persistence': 'reset'}),
    preset({'preset': 'reward', 'key': 'ec_reward', 'x': 616, 'z': 409, 'donor_id': 0, 'appearance': 341,
            'give': {'badge': 3}, 'state': {'new': 'ec_reward_given', 'name': 'Reward given'}}),
    preset({'preset': 'puzzle_reset', 'key': 'ec_reset', 'x': 618, 'z': 411, 'donor_id': 0, 'appearance': 333,
            'destination': {'x': 620, 'z': 410, 'facing': 0}}),
    preset({'preset': 'field_obstacle', 'key': 'ec_tree', 'x': 622, 'z': 409, 'donor_id': 0, 'family': 'cut',
            'authority': {'kind': 'badge', 'badge': 1}})]


def test_presets_expand_to_ordinary_story_transactions(tmp_path):
    p = fresh(tmp_path)
    plan = p.plan_area_edit(PRESETS)
    assert [(t['kind'], t['key']) for t in plan['transactions']] == [
        ('state', 'ec_gate_open'), ('sequence', 'ec_gate'), ('sequence', 'ec_gate_switch'),
        ('state', 'ec_potion_taken'), ('sequence', 'ec_potion'), ('sequence', 'ec_berry'),
        ('state', 'ec_reward_given'), ('sequence', 'ec_reward'), ('sequence', 'ec_reset'), ('sequence', 'ec_tree')]
    assert all(t['schema'] in sa.SCHEMAS for t in plan['transactions'])      # no preset-specific schema
    p.apply_area_edit(64, operations=PRESETS)
    state = p.composed()
    seqs, states = sa.catalog(state, 'sequence'), sa.catalog(state, 'state')
    assert seqs['ec_gate']['field_flag'] == states['ec_gate_open']['flag']
    assert seqs['ec_potion']['field_flag'] == states['ec_potion_taken']['flag']
    assert seqs['ec_berry']['field_flag'] in fm.MAP_TEMP_FLAGS
    raw = sa.replacements(p, state)[fmt.SCRIPT_ARCHIVE][seqs['ec_gate']['script_member']]
    ids = sa.allocation(p, state)

    def ops(key):
        return [c for c in names(sd.disassemble(raw, [sd.entries(raw)[ids[key][0] - 1]]))]
    switch = ops('ec_gate_switch')
    assert ('hide_person', (seqs['ec_gate']['npc_id'],)) in switch and ('setflag', (states['ec_gate_open']['flag'],)) in switch
    # A pickup is hidden and marked only on the successful grant branch (no duplicate grant, full Bag keeps it).
    pickup = ops('ec_potion')
    at = [n for n, _ in pickup]
    assert at.index('giveitem') < at.index('setflag') < at.index('hide_person')
    assert ('give_badge', (3,)) in ops('ec_reward')
    assert ('warp', (33, 0, 620, 410, 0)) in ops('ec_reset')
    # Deleting a gate that a switch removes is refused by whole-project validation.
    with pytest.raises(EditorError) as e:
        p.plan_area_edit([{'kind': 'story', 'context': R29, 'request': {'kind': 'sequence', 'key': 'ec_gate',
                                                                          'action': 'delete', 'value': None}}])
    assert e.value.code == 'INVALID_EVENT'
    with pytest.raises(EditorError):
        p.plan_area_edit([preset({'preset': 'teleporter', 'key': 'x'})])


# ---- UI parity (presets, field objects, new steps) --------------------------------------------

def test_story_editor_presets_field_objects_and_new_steps(tmp_path):
    import os
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtWidgets import QApplication
    from sovereign_editor.map_inspector import MapInspectorWindow
    from sovereign_editor.story_ui import StoryEditor
    from sovereign_editor.gui import STYLE
    app = QApplication.instance() or QApplication([]); app.setStyle('Fusion'); app.setStyleSheet(STYLE)
    p = fresh(tmp_path)
    w = MapInspectorWindow(p, context=(33, [19, 12])); w.show(); app.processEvents()
    d = StoryEditor(w); d.show(); app.processEvents()
    out = ROOT / 'evidence/editor-completion-v1/ui'; out.mkdir(parents=True, exist_ok=True)
    before = p.path.read_bytes()
    d.tabs.setCurrentIndex(4)
    d.preset_kind.setCurrentIndex(d.preset_kind.findData('pickup')); d.preset_key.setText('ui_potion')
    d.preset_x.setValue(608); d.preset_z.setValue(409); d.preset_donor.setValue(0)
    d.preset_persistence.setCurrentIndex(1); d.preset_state_mode.setCurrentIndex(0)
    d.preset_state_key.setText('ui_potion_taken'); d.preset_state_name.setText('UI potion taken'); d.preset_item.setValue(17)
    app.processEvents(); assert d.preset_form.isRowVisible(d.preset_state_key) and not d.preset_form.isRowVisible(d.preset_family)
    d.stage_preset(); assert d.apply_button.isEnabled() and p.path.read_bytes() == before, d.status.text()
    d.preset_kind.setCurrentIndex(d.preset_kind.findData('field_obstacle')); d.preset_key.setText('ui_tree')
    d.preset_x.setValue(622); d.preset_family.setCurrentIndex(d.preset_family.findData('cut'))
    d.preset_persistence.setCurrentIndex(0); d.preset_authority.setCurrentIndex(1)
    d.preset_auth_badge.setCurrentIndex(d.preset_auth_badge.findData(1)); app.processEvents()
    assert d.preset_form.isRowVisible(d.preset_auth_badge) and not d.preset_form.isRowVisible(d.preset_auth_money)
    d.stage_preset(); assert p.path.read_bytes() == before, d.status.text()
    assert d.grab().save(str(out / 'presets.png'))
    # The staged objects load back into the Events tab with their field block.
    d.tabs.setCurrentIndex(3); d.events.setCurrentIndex(d.events.findData('ui_potion')); app.processEvents()
    assert (d.field_family.currentData(), d.field_look.currentData(), d.field_persistence.currentData(),
            d.field_state.currentData()) == ('object', 'ball', 'permanent', 'ui_potion_taken')
    assert not d.event_form.isRowVisible(d.event_character)
    d.events.setCurrentIndex(d.events.findData('ui_tree')); app.processEvents()
    assert d.field_family.currentData() == 'cut' and [n['op'] for n in d.nodes][:2] == ['require', 'require']
    # A warp step through the step editor.
    d.step_type.setCurrentText('warp'); d.add_step(); d.step_header.setValue(67); d.step_destination.setText('570, 399')
    d.step_direction.setCurrentIndex(0); d.save_step()
    assert d.nodes[-1] == {'id': d.nodes[-1]['id'], 'op': 'warp', 'header': 67, 'x': 570, 'z': 399, 'facing': 0}
    assert not d.step_form.isRowVisible(d.next_step)
    d.resize(1000, 720); app.processEvents(); assert d.grab().save(str(out / 'events-field-compact.png'))
    d.apply()
    after = Project(p.root)
    seqs = after.story_library()['sequences']
    assert {'ui_potion', 'ui_tree'} <= set(seqs) and after.doc['revision'] == 65
    w.close(); app.processEvents()


# ---- trainers (BATTLE-01..05) -----------------------------------------------------------------

from sovereign_editor import trainer_format as tf, native_trainers as nt  # noqa: E402


def mon(species, level, **extra):
    return {'species': species, 'level': level, 'moves': None, 'held_item': 0, **extra}


def v2_trainer(key, name, party, **extra):
    value = {'name': name, 'character': None, 'stock_class': 2, 'party': party, 'before': ['Our eyes met!'],
             'after': ['Good battle.'], 'revisit': ['I will train more.'], 'policy': tf.POLICY,
             'defeat': ['I lost!'], **extra}
    return {'kind': 'story', 'context': R29, 'request': {'kind': 'trainer', 'key': key, 'action': 'put', 'value': value}}


def placement(key, trainer, x, z, facing, sight, partner=False, movement=0):
    return {'kind': 'story', 'context': R29, 'request': {'kind': 'sequence', 'key': key, 'action': 'put', 'value': {
        'kind': 'trainer', 'x': x, 'z': z, 'donor_id': 0, 'facing': facing, 'movement': movement, 'range_x': 0,
        'range_z': 0, 'character': None, 'nodes': [], 'once_state': None, 'trainer': trainer, 'sight': sight,
        'partner': partner, 'stock_sprite': 315}}}


TRAINERS = [v2_trainer('ranger_01', 'Joey II', [mon(161, 8, nature=3, ivs=[31] * 6, evs=[0] * 6, ability='hidden')]),
            v2_trainer('ranger_02', 'Twins', [mon(16, 7), mon(19, 7)], battle='double',
                       insufficient=['We battle two at once!']),
            placement('ec_sight1', 'ranger_01', 620, 409, 2, 3),
            placement('ec_twin_a', 'ranger_02', 612, 410, 3, 2),
            placement('ec_twin_b', 'ranger_02', 612, 409, 3, 2, partner=True)]


def test_v2_trainer_layout_and_refusals(tmp_path):
    p = fresh(tmp_path)
    t = {'party': [mon(161, 30, nature=3, ability='hidden', ball=2, ivs=[31, 30, 29, 28, 27, 26], evs=[252, 0, 4, 252, 0, 0]),
                   mon(16, 25, nature=15, ability='second', ball=4, ivs=[0] * 6, evs=[0] * 6)],
         'ai': ['prioritize_super_effective', 'evaluate_attacks'], 'items': [17], 'battle': 'double'}
    tf.validate(p, t)
    header, party = tf.encode(t, 2)
    back = tf.decode(header, party)
    assert (back['data_type'], back['ai'], back['double'], back['items']) == (0x38, 3, 2, [17])
    assert [(m['nature'], m['ability_slot'], m['ball'], m['ivs'], m['evs']) for m in back['party']] == \
        [(3, 2, 2, [31, 30, 29, 28, 27, 26], [252, 0, 4, 252, 0, 0]), (15, 1, 4, [0] * 6, [0] * 6)]
    refusals = [({'party': [mon(545, 5, ability='second')]}, 'UNSUPPORTED_ABILITY'),     # Snivy: no second ability
                ({'party': [mon(161, 5, ivs=[0] * 6, evs=[252, 252, 7, 0, 0, 0])]}, 'INVALID_INPUT'),
                ({'party': [mon(161, 5, ivs=[0] * 6)]}, 'INVALID_INPUT'),                 # IVs without EVs
                ({'party': [mon(161, 5, nature=25)]}, 'INVALID_INPUT'),
                ({'party': [mon(161, 5)], 'battle': 'double'}, 'INVALID_INPUT'),
                ({'party': [mon(161, 5, ball=4), mon(16, 5)]}, 'INVALID_INPUT'),          # per-team layout
                ({'party': [mon(161, 5)], 'ai': ['catching_demo']}, 'INVALID_INPUT')]
    for trainer, code in refusals:
        with pytest.raises(EditorError) as e:
            tf.validate(p, trainer)
        assert e.value.code == code


def test_native_sight_trainers_records_messages_and_std_entries(tmp_path):
    p = fresh(tmp_path)
    p.apply_area_edit(64, operations=TRAINERS, label='trainers')
    state = p.composed()
    seqs, library = sa.catalog(state, 'sequence'), sa.catalog(state, 'trainer')
    rows = {r['id']: r for r in ev.records(ev.raw_member(p, seqs['ec_sight1']['event_member'], state)) if r['kind'] == 'npc'}
    joey, twins = library['ranger_01']['trainer_id'], library['ranger_02']['trainer_id']
    for key, script_id, sight in (('ec_sight1', 3000 + joey - 1, 3), ('ec_twin_a', 3000 + twins - 1, 2),
                                  ('ec_twin_b', 5000 + twins - 1, 2)):
        raw = rows[seqs[key]['npc_id']]['raw']
        assert struct.unpack_from('<3H', raw, 6)[0] == 1 and rows[seqs[key]['npc_id']]['script'] == script_id
        assert struct.unpack_from('<H', raw, 14)[0] == sight
    tables = sa.replacements(p, state)
    stock_bank = p.resource(fmt.SCRIPT_ARCHIVE, 953)[1]
    bank = tables[fmt.SCRIPT_ARCHIVE][953]
    entries, stock_entries = sd.entries(bank), sd.entries(stock_bank)
    assert len(entries) == max(joey, twins)
    flow = lambda raw, at: [(op, tuple(a)) for _, (op, _, a, _) in sorted(sd.disassemble(raw, [at]).items())]
    assert flow(bank, entries[twins - 1]) == flow(stock_bank, stock_entries[0])
    assert flow(bank, entries[739]) == flow(stock_bank, stock_entries[739])       # special entry kept
    stock_table = p.resource(nt.TRTBL, 0)[1]
    table = tables[nt.TRTBL][0]
    records = [struct.unpack_from('<HH', table, i) for i in range(len(stock_table), len(table), 4)]
    assert records == [(joey, 0), (joey, 1), (joey, 2)] + [(twins, k) for k in range(3, 11)]
    offsets = tables[nt.TRTBL_OFFSETS][0]
    assert struct.unpack_from('<H', offsets, 2 * joey)[0] == len(stock_table)
    assert len(fmt.text_entries(tables[fmt.TEXT_ARCHIVE][nt.TRAINER_TEXT])[1]) * 4 == len(table)
    # Sight lines must be clear, pairs complete, singles alone; slot 740 never native.
    special = next(k for k, t in library.items() if t['trainer_id'] == nt.SPECIAL_TRAINER)
    solo = v2_trainer('ranger_03', 'Solo', [mon(161, 5)])
    for ops, code in (([solo, placement('bad', 'ranger_03', 608, 409, 2, 3)], 'INVALID_SIGHT'),    # sight into the wall
                      ([solo, placement('bad', 'ranger_03', 624, 409, 2, 4)], 'INVALID_SIGHT'),    # sight reaches ec_sight1
                      ([placement('bad', 'ranger_01', 616, 411, 1, 1)], 'INVALID_EVENT'),          # placed twice
                      ([{'kind': 'story', 'context': R29, 'request': {'kind': 'sequence', 'key': 'ec_twin_b',
                                                                        'action': 'delete', 'value': None}}], 'INVALID_EVENT'),
                      ([placement('bad', 'ranger_01', 616, 409, 1, 1, partner=True)], 'INVALID_INPUT'),
                      ([v2_trainer(special, 'Slot740', [mon(161, 5)]), placement('bad', special, 616, 409, 1, 1)],
                       'UNSUPPORTED_RUNTIME')):
        with pytest.raises(EditorError) as e:
            p.plan_area_edit(ops)
        assert e.value.code == code, (code, str(e.value))


def test_double_talk_battle_and_conditional_rematch_compile(tmp_path):
    p = fresh(tmp_path)
    ops = TRAINERS + [
        v2_trainer('ranger_03', 'Joey III', [mon(162, 20), mon(161, 18)], battle='double'),       # repeatable rematch
        preset({'preset': 'rematch', 'key': 'ec_rematch', 'x': 616, 'z': 409, 'donor_id': 0, 'appearance': 315,
                'original': 'ranger_01', 'trainer': 'ranger_03', 'condition': {'kind': 'badge', 'badge': 0}})]
    p.apply_area_edit(64, operations=ops, label='rematch')
    state = p.composed()
    seqs, library = sa.catalog(state, 'sequence'), sa.catalog(state, 'trainer')
    raw = sa.replacements(p, state)[fmt.SCRIPT_ARCHIVE][seqs['ec_rematch']['script_member']]
    ops_ = names(sd.disassemble(raw, [sd.entries(raw)[sa.allocation(p, state)['ec_rematch'][0] - 1]]))
    assert ('checktrainerflag', (library['ranger_01']['trainer_id'],)) in ops_       # native original: trainer flag
    assert ('check_badge', (0, 0x800C)) in ops_
    assert ('party_check_for_double', (0x800C,)) in ops_                             # double: native two-Pokémon check
    # One NPC holds both battles (R82-REMATCH-01): the check precedes the double rematch battle.
    order = [n for n, _ in ops_]
    rematch = ops_.index(('trainer_battle', (library['ranger_03']['trainer_id'], 0, 0, 0)))
    assert order.index('party_check_for_double') < rematch
    # Repeatable: no defeat state is tested or set around this battle.
    states = {v['flag'] for v in sa.catalog(state, 'state').values() if v.get('switch')}
    assert not any(n == 'setflag' and a[0] in states for n, a in ops_)


def test_story_editor_v2_trainers_sight_placement_and_rematch_rows(tmp_path):
    import os
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtWidgets import QApplication
    from sovereign_editor.map_inspector import MapInspectorWindow
    from sovereign_editor.story_ui import StoryEditor
    from sovereign_editor.gui import STYLE
    app = QApplication.instance() or QApplication([]); app.setStyle('Fusion'); app.setStyleSheet(STYLE)
    p = fresh(tmp_path)
    w = MapInspectorWindow(p, context=(33, [19, 12])); w.show(); app.processEvents()
    d = StoryEditor(w); d.show(); app.processEvents()
    out = ROOT / 'evidence/editor-completion-v1/ui'; out.mkdir(parents=True, exist_ok=True)
    before = p.path.read_bytes()
    d.tabs.setCurrentIndex(1); d.trainers.setCurrentIndex(d.trainers.findData('ranger_01')); app.processEvents()
    d.trainer_policy.setCurrentIndex(d.trainer_policy.findData(tf.POLICY))
    d.trainer_character.setCurrentIndex(d.trainer_character.findData('stock:2')); d.trainer_name.setText('Joey II')
    d.trainer_defeat.setCurrentIndex(0)                                   # repeatable talk battle / native flag
    d.team_natures.setChecked(True); app.processEvents()
    assert not d.party.isColumnHidden(7) and d.party.isColumnHidden(10)
    d.party.cellWidget(0, 7).setCurrentIndex(3); d.party.cellWidget(0, 8).setCurrentIndex(2)
    d.trainer_defeat_text.setPlainText('I lost!')
    d.stage_trainer(); assert p.path.read_bytes() == before, d.status.text()
    staged = d.preview.story_library()['trainers']['ranger_01']
    assert staged['policy'] == tf.POLICY and staged['party'][0]['nature'] == 3 and staged['party'][0]['ability'] == 'hidden'
    assert 'defeat_state' not in staged and staged['defeat'] == ['I lost!']
    d.resize(1200, 820); app.processEvents(); assert d.grab().save(str(out / 'trainers-v2.png'))
    d.tabs.setCurrentIndex(3); d.events.setCurrentIndex(0); d.event_key.setText('ui_sight'); d.entry.setCurrentIndex(3)
    app.processEvents(); assert d.event_form.isRowVisible(d.sight_trainer) and not d.event_form.isRowVisible(d.once)
    d.x.setValue(620); d.z.setValue(409); d.facing.setCurrentIndex(2); d.donor.setValue(0)
    d.event_character.setCurrentIndex(d.event_character.findData('stock:315'))
    d.sight_trainer.setCurrentIndex(d.sight_trainer.findData('ranger_01')); d.sight_range.setValue(3)
    d.stage_event(); assert p.path.read_bytes() == before, d.status.text()
    assert d.preview.story_library()['sequences']['ui_sight']['kind'] == 'trainer'
    d.tabs.setCurrentIndex(4); d.preset_kind.setCurrentIndex(d.preset_kind.findData('rematch')); app.processEvents()
    assert d.preset_form.isRowVisible(d.preset_original) and d.preset_form.isRowVisible(d.preset_rematch)
    d.apply()
    after = Project(p.root)
    assert after.story_library()['sequences']['ui_sight']['trainer'] == 'ranger_01' and after.doc['revision'] == 65
    w.close(); app.processEvents()


# ---- gameplay records (DATA-01..03) ------------------------------------------------------------

from sovereign_editor import game_data as gd, world_authoring as wa, character_runtime as crt, species as sp  # noqa: E402
from sovereign_editor.formats import digest  # noqa: E402


def data_op(ops, label='data'):
    return {'kind': 'data', 'context': R29, 'request': {'operations': ops, 'label': label}}


def test_records_catalogs_and_layout_readback(tmp_path):
    p = fresh(tmp_path)
    state = p.composed()
    potion, ether, hp_up = (gd.decode_item(gd._members(p, state, gd.ITEMS)(i)) for i in (17, 38, 45))
    assert (potion['price'], potion['hp_restore'], ether['pp_restore'], hp_up['ev_gain'][0]) == (200, 20, 10, 10)
    surf = gd.decode_move(gd._members(p, state, gd.MOVES)(57))
    assert (surf['power'], surf['type'], surf['category'], surf['pp'], surf['accuracy']) == (90, 11, 1, 15, 100)
    moves = {e['id']: e for e in gd.catalog(p, 'moves', limit=600, state=state)['entries']}
    assert moves[471]['supported'] and moves[471]['expanded']                      # Hone Claws
    assert 'FLAG_UNUSED_MOVE' in moves[475]['reason'] and 'Placeholder' in moves[468]['reason']
    abilities = {e['id']: e for e in gd.catalog(p, 'abilities', limit=600, state=state)['entries']}
    assert abilities[126]['supported'] and abilities[1]['supported']
    assert not all(e['supported'] for e in abilities.values())                    # unreferenced expanded ones refused
    assert gd.item_entry(p, state, 538)['holdable'] and not gd.item_entry(p, state, 17)['holdable']
    tms = gd.machines(p, state)
    assert len(tms) == 100 and tms[0]['name'] == 'TM001 · Focus Punch' and not tms[92]['supported']


def test_data_edits_apply_patch_runtime_and_refuse(tmp_path):
    p = fresh(tmp_path)
    state = p.composed()
    tackle, potion = gd._members(p, state, gd.MOVES)(33), gd._members(p, state, gd.ITEMS)(17)
    ops = [{'kind': 'move', 'id': 33, 'before_sha256': digest(tackle), 'changes': {'power': 50, 'priority': 1}},
           {'kind': 'item', 'id': 17, 'before_sha256': digest(potion), 'changes': {'price': 250, 'hp_restore': 25}},
           {'kind': 'machine', 'index': 0, 'before': 264, 'move': 471}]
    before = p.path.read_bytes()
    plan = p.plan_area_edit([data_op(ops)])
    assert p.path.read_bytes() == before
    impact = {pv['kind']: pv['impact'] for pv in plan['transactions'][0]['preview']}
    assert impact['move']['level_up_species'] > 100 and impact['machine']['compatible_species'] > 100
    p.apply_area_edit(64, operations=[data_op(ops)])
    state = p.composed()
    assert gd.decode_move(gd._members(p, state, gd.MOVES)(33))['power'] == 50
    runtime = wa.runtime_plan(p, state)
    info = crt.overlay(p.blob, 129)
    assert struct.unpack_from('<H', runtime['files'][info['file_id']], sp.MACHINE_TABLE - info['address'])[0] == 471
    for bad, code in (([{'kind': 'move', 'id': 33, 'before_sha256': digest(tackle), 'changes': {'power': 60}}], 'BEFORE_VALUE_MISMATCH'),
                      ([{'kind': 'machine', 'index': 92, 'before': 15, 'move': 33}], 'UNSUPPORTED_MACHINE'),   # HM01
                      ([{'kind': 'move', 'id': 475, 'before_sha256': '', 'changes': {'power': 1}}], 'UNSUPPORTED_ID'),
                      ([{'kind': 'item', 'id': 4, 'before_sha256': digest(gd._members(p, state, gd.ITEMS)(4)),
                         'changes': {'hp_restore': 30}}], 'UNSUPPORTED_ITEM_USE'),                           # Poké Ball
                      ([{'kind': 'move', 'id': 45, 'before_sha256': digest(gd._members(p, state, gd.MOVES)(45)),
                         'changes': {'effect': 999}}], 'UNSUPPORTED_EFFECT')):
        with pytest.raises(EditorError) as e:
            p.plan_area_edit([data_op(bad)])
        assert e.value.code == code, (code, str(e.value))
    p.undo(65)
    assert gd.decode_move(gd._members(p, p.composed(), gd.MOVES)(33))['power'] == 40


def test_records_tab_stages_previews_and_applies(tmp_path):
    import os
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtWidgets import QApplication
    from sovereign_editor.gameplay_ui import GameplayEditor
    from sovereign_editor.gui import STYLE
    app = QApplication.instance() or QApplication([]); app.setStyle('Fusion'); app.setStyleSheet(STYLE)
    p = fresh(tmp_path)
    d = GameplayEditor(p, header=33); d.show(); app.processEvents()
    d.tabs.setCurrentWidget(d.records); r = d.records
    r.move.setCurrentIndex(r.move.findData(33)); app.processEvents()
    assert r.power.value() == 40
    r.power.setValue(55); r.stage_move()
    r.item.setCurrentIndex(r.item.findData(17)); app.processEvents()
    assert r.item_form.isRowVisible(r.hp) and not r.item_form.isRowVisible(r.pp_restore)
    r.price.setValue(260); r.stage_item()
    before = p.path.read_bytes(); r.preview_records(); assert p.path.read_bytes() == before, r.status.text()
    assert 'Tackle' in r.preview.toPlainText() and 'learnsets' in r.preview.toPlainText()
    r.catalog_kind.setCurrentText('abilities'); r.catalog_only.setCurrentIndex(1); app.processEvents()
    assert r.catalog_list.count() > 0 and 'refused' in r.catalog_list.item(0).text()
    out = ROOT / 'evidence/editor-completion-v1/ui'; out.mkdir(parents=True, exist_ok=True)
    assert d.grab().save(str(out / 'records.png'))
    r.apply(); app.processEvents()
    assert Project(p.root).data_record('move', 33)['record']['power'] == 55
    d.close(); app.processEvents()


# ---- shops, healing and respawn (SERVICE-01) -------------------------------------------------

sys_path_tools = str(ROOT / 'tools')
import sys  # noqa: E402
if sys_path_tools not in sys.path:
    sys.path.insert(0, sys_path_tools)
import mart_qualification as mq  # noqa: E402
from sovereign_editor import field_services as fsv  # noqa: E402


def npc(key, x, z, nodes, sprite=333):
    return {'kind': 'story', 'context': R29, 'request': {'kind': 'sequence', 'key': key, 'action': 'put', 'value': {
        'kind': 'npc', 'x': x, 'z': z, 'donor_id': 0, 'facing': 1, 'movement': 0, 'range_x': 0, 'range_z': 0,
        'character': None, 'stock_sprite': sprite, 'once_state': None, 'nodes': nodes}}}


HERBS = [17, 18, 4, 26]
SERVICE_OPS = [
    data_op([{'kind': 'shop', 'name': 'ec_herbs', 'before': None, 'items': HERBS}]),
    npc('ec_clerk', 634, 410, [{'id': 'buy', 'op': 'shop', 'shop': 'ec_herbs', 'next': 'bye'},
                               {'id': 'bye', 'op': 'say', 'pages': ['Come again!'], 'next': 'done'},
                               {'id': 'done', 'op': 'end', 'complete': False}]),
    npc('ec_nurse', 636, 410, [{'id': 'ask', 'op': 'choice', 'pages': ['Rest here?'], 'yes': 'heal', 'no': 'done'},
                               {'id': 'heal', 'op': 'heal', 'next': 'spawn'},
                               {'id': 'spawn', 'op': 'set_spawn', 'spawn': 2, 'next': 'done'},
                               {'id': 'done', 'op': 'end', 'complete': False}])]


def test_shops_heal_and_respawn_compile_and_resolve_natively(tmp_path):
    p = fresh(tmp_path)
    plan = p.plan_area_edit(SERVICE_OPS)
    shop_preview = plan['transactions'][0]['preview'][0]
    assert shop_preview['after'] == HERBS and any('0x9A' in n for n in shop_preview['impact']['notes'])
    p.apply_area_edit(64, operations=SERVICE_OPS, label='services')
    state = p.composed()
    assert fsv.shop_index(state) == {'ec_herbs': 30}
    raw = sa.replacements(p, state)[fmt.SCRIPT_ARCHIVE][sa.catalog(state, 'sequence')['ec_clerk']['script_member']]
    ids = sa.allocation(p, state)
    clerk = names(sd.disassemble(raw, [sd.entries(raw)[ids['ec_clerk'][0] - 1]]))
    at = clerk.index(('callstd', (2011,)))
    assert clerk[at:at + 4] == [('callstd', (2011,)), ('holdmsg', ()), ('setvar', (0x8004, 30)), ('callstd', (2052,))]
    nurse = names(sd.disassemble(raw, [sd.entries(raw)[ids['ec_nurse'][0] - 1]]))
    # std 2069 (script member 3, entry 69) command for command, then the chosen spawn.
    stock_raw = p.resource(fmt.SCRIPT_ARCHIVE, 3)[1]
    stock = [c for c in names(sd.disassemble(stock_raw, [sd.entries(stock_raw)[69]])) if c[0] != 'endstd']
    at = nurse.index(stock[0])
    assert nurse[at:at + len(stock)] == stock and ('set_spawn', (2,)) in nurse
    # The ROM's own special-mart command and Mart_Init list helpers resolve the authored shop.
    runtime = wa.runtime_plan(p, state)
    assert [r['index'] for r in runtime['shops']['rows']] == [30]
    looked = {(r['index'], r['pokeball_flag']): r for r in mq.run(mq.memory_from_plan(p.blob, runtime), [3, 30])}
    assert looked[(30, 1)]['items'] == HERBS and looked[(30, 0)]['items'] == [17, 18, 26]
    stock3 = fsv.stock_marts(p.blob)[3]['items']
    assert looked[(3, 1)]['items'] == stock3 and looked[(3, 0)]['items'] == [i for i in stock3 if i != 4]
    # Shared-resource guards: a used shop cannot be deleted; a sold item keeps a price.
    potion = gd._members(p, state, gd.ITEMS)(17)
    for ops, code in (([{'kind': 'shop', 'name': 'ec_herbs', 'before': HERBS, 'items': None}], 'IN_USE'),
                      ([{'kind': 'item', 'id': 17, 'before_sha256': digest(potion), 'changes': {'price': 0}}], 'IN_USE'),
                      ([{'kind': 'shop', 'name': 'ec_herbs', 'before': None, 'items': [17]}], 'BEFORE_VALUE_MISMATCH')):
        with pytest.raises(EditorError) as e:
            p.plan_area_edit([data_op(ops)])
        assert e.value.code == code, (code, str(e.value))
    # Revising the inventory reaches every clerk through the same table slot.
    p.apply_area_edit(65, operations=[data_op([{'kind': 'shop', 'name': 'ec_herbs', 'before': HERBS, 'items': [26, 28]}])])
    runtime = wa.runtime_plan(p, p.composed())
    assert mq.run(mq.memory_from_plan(p.blob, runtime), [30])[1]['items'] == [26, 28]
    p.undo(66)
    assert fsv.shops(p.composed())['ec_herbs'] == HERBS


def test_service_refusals_and_cli(tmp_path):
    p = fresh(tmp_path)
    for items, code in (([431], 'UNSUPPORTED_ID'),       # Poké Radar: key item
                        ([1], 'UNSUPPORTED_ID'),         # Master Ball: no price
                        ([425], 'UNSUPPORTED_ID'),       # HM06
                        ([17, 17], 'INVALID_INPUT'), ([], 'INVALID_INPUT')):
        with pytest.raises(EditorError) as e:
            p.plan_area_edit([data_op([{'kind': 'shop', 'name': 'bad', 'before': None, 'items': items}])])
        assert e.value.code == code, (items, str(e.value))
    shop = SERVICE_OPS[0]
    battle_clerk = npc('bad', 634, 410, [{'id': 'buy', 'op': 'shop', 'shop': 'ec_herbs', 'next': 'fight'},
                                         {'id': 'fight', 'op': 'battle', 'trainer': 'practice_ari', 'won': 'done', 'lost': 'done'},
                                         {'id': 'done', 'op': 'end', 'complete': False}])
    lake = npc('bad', 634, 410, [{'id': 's', 'op': 'set_spawn', 'spawn': 10, 'next': 'done'},
                                 {'id': 'done', 'op': 'end', 'complete': False}])
    for ops, code in (([battle_clerk], 'INVALID_EVENT'),
                      ([shop, battle_clerk], 'INVALID_EVENT'),
                      ([SERVICE_OPS[1]], 'NOT_FOUND'),           # shop not defined yet
                      ([lake], 'UNSUPPORTED_ID')):               # Lake of Rage is not a blackout spawn
        with pytest.raises(EditorError) as e:
            p.plan_area_edit(ops)
        assert e.value.code == code, (code, str(e.value))
        assert battle_clerk not in ops or 'own clerk event' in str(e.value)
    spawns = run_cli('data-catalog', '--project', p.path.parent, '--kind', 'spawns', '--limit', 40)['result']
    rows = {r['id']: r for r in spawns['entries']}
    assert rows[2]['map'] == 'T21PC0101' and rows[2]['supported'] and not rows[10]['supported'] and spawns['total'] == 30
    request = tmp_path / 'shop.json'
    request.write_text(json.dumps({'operations': [{'kind': 'shop', 'name': 'ec_herbs', 'before': None, 'items': HERBS}],
                                   'label': 'Herb shop'}))
    dry = run_cli('data-edit', '--project', p.path.parent, '--request', request, '--dry-run')['result']
    assert dry['preview'][0]['operation'] == 'data.transaction' and dry['preview'][0]['preview'][0]['after'] == HERBS
    applied = run_cli('data-edit', '--project', p.path.parent, '--request', request, '--revision', 64)['result']
    assert applied['revision'] == 65
    record = run_cli('data-record', '--project', p.path.parent, '--kind', 'shop', '--name', 'ec_herbs')['result']
    assert record['before'] == HERBS and record['record']['id'] == 30
    shops = run_cli('data-catalog', '--project', p.path.parent, '--kind', 'shops')['result']
    assert shops['entries'][0]['item_names'][:2] == ['Potion', 'Antidote']


def test_shop_records_and_service_steps_in_the_editors(tmp_path):
    import os
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtWidgets import QApplication
    from sovereign_editor.gameplay_ui import GameplayEditor
    from sovereign_editor.map_inspector import MapInspectorWindow
    from sovereign_editor.story_ui import StoryEditor
    from sovereign_editor.gui import STYLE
    app = QApplication.instance() or QApplication([]); app.setStyle('Fusion'); app.setStyleSheet(STYLE)
    p = fresh(tmp_path)
    out = ROOT / 'evidence/editor-completion-v1/ui'; out.mkdir(parents=True, exist_ok=True)
    g = GameplayEditor(p, header=33); g.show(); app.processEvents()
    g.tabs.setCurrentWidget(g.records); r = g.records
    assert r.shop.currentData() is None and r.shop_pick.findData(431) < 0 and r.shop_pick.findData(1) < 0   # key/unpriced
    r.shop_name.setText('ui_herbs')
    for item in (17, 18, 4):
        r.shop_pick.setCurrentIndex(r.shop_pick.findData(item)); r.add_shop_item()
    r.stage_shop(); before = p.path.read_bytes(); r.preview_records()
    assert p.path.read_bytes() == before and 'ui_herbs' in r.preview.toPlainText() and '0x9A' in r.preview.toPlainText()
    r.catalog_kind.setCurrentText('spawns'); app.processEvents()
    assert any('refused: Not a blackout spawn' in r.catalog_list.item(i).text() for i in range(r.catalog_list.count()))
    g.resize(1180, 820); app.processEvents(); assert g.grab().save(str(out / 'records-shops.png'))
    r.apply(); app.processEvents()
    assert r.shop.findData('ui_herbs') > 0 and Project(p.root).data_record('shop', 'ui_herbs')['before'] == [17, 18, 4]
    g.close(); app.processEvents()
    w = MapInspectorWindow(Project(p.root), context=(33, [19, 12])); w.show(); app.processEvents()
    d = StoryEditor(w); d.show(); app.processEvents()
    d.tabs.setCurrentIndex(3); d.events.setCurrentIndex(0); d.event_key.setText('ui_clerk'); d.entry.setCurrentIndex(0)
    d.x.setValue(634); d.z.setValue(410); d.donor.setValue(0)
    d.event_character.setCurrentIndex(d.event_character.findData('stock:333'))
    d.nodes = []
    for op in ('shop', 'heal', 'set_spawn', 'end'):
        d.step_type.setCurrentText(op); d.add_step(); app.processEvents()
        if op == 'shop':
            assert d.scene_form.isRowVisible(d.step_shop) and not d.scene_form.isRowVisible(d.step_spawn)
            d.step_shop.setCurrentIndex(d.step_shop.findData('ui_herbs'))
        if op == 'set_spawn':
            assert d.step_spawn.findData(10) < 0                                        # Lake of Rage: not a blackout spawn
            d.step_spawn.setCurrentIndex(d.step_spawn.findData(2))
        if op != 'end':
            d.next_step.setText(f'step{len(d.nodes) + 1}')
        d.save_step()
    assert [n['op'] for n in d.nodes] == ['shop', 'heal', 'set_spawn', 'end']
    assert d.nodes[0]['shop'] == 'ui_herbs' and d.nodes[2]['spawn'] == 2
    d.resize(1000, 720); d.steps.setCurrentRow(0); app.processEvents()
    from PySide6.QtWidgets import QScrollArea
    area = d.tabs.currentWidget()
    if isinstance(area, QScrollArea):
        area.ensureWidgetVisible(d.step_shop); app.processEvents()
    assert d.grab().save(str(out / 'events-services-compact.png'))
    before = Path(p.path).read_bytes()
    d.stage_event(); assert Path(p.path).read_bytes() == before, d.status.text()
    d.apply()
    assert Project(p.root).story_library()['sequences']['ui_clerk']['nodes'][0]['shop'] == 'ui_herbs'
    w.close(); app.processEvents()


def test_native_field_authority_is_read_from_this_rom():
    blob = Project(PARENT).blob
    q = fm.qualify_field_authority(blob)
    assert {r['action']: r['badge'] for r in q['field_menu']} == {
        'cut': 1, 'fly': 4, 'surf': 3, 'strength': 2, 'rock_smash': 0, 'waterfall': 7, 'rock_climb': 15, 'whirlpool': 6}
    assert q['water_prompt']['badge'] == fm.SURF['badge'] and q['water_prompt']['script'] == 10004
    # The script-level families agree with the field-code checks.
    assert all(fm.FAMILIES[f]['badge'] == {r['action']: r['badge'] for r in q['field_menu']}[f] for f in fm.FAMILIES)
    tampered = bytearray(blob)
    start = struct.unpack_from('<I', blob, 0x20)[0]
    tampered[start + 0x020680FA - 2 - 0x02000000] = 0x04            # movs r1,#4 before the Surf badge test
    with pytest.raises(EditorError):
        fm.qualify_field_authority(bytes(tampered))
