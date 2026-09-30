"""Original content v1 (docs/ORIGINAL_CONTENT_V1_*): software checks only, not native acceptance."""
import struct
from pathlib import Path

import pytest

from sovereign_editor import dialogue_format as fmt, event_sequences as seq, presets, script_disasm as sd
from sovereign_editor.formats import EditorError

ROOT = Path(__file__).resolve().parents[1]
PARENT = ROOT / 'projects/editor-completion-v1'          # r82 parent, read-only here


def listing(code):
    """(name, args) of every command reachable from the compiled sequence's first byte."""
    return [(name, tuple(args)) for _, (op, name, args, _) in sd.disassemble(code, [0]).items()]


def expand(request, context=None):
    return [op['request']['value'] for op in presets.expand(None, context or {'header': 1, 'cell': [0, 0]}, request)
            if op['request']['kind'] == 'sequence']


# ---- R82-TEXT-01: trainer intros wait for a press -------------------------------------------------

def test_held_pages_end_with_the_stock_page_break():
    plain = fmt.encode_pages(['We always battle\nside by side!'])
    held = fmt.encode_pages(['We always battle\nside by side!'], wait=True)
    assert plain[-1] == 0xFFFF and plain[-2] != fmt.PAGE_BREAK
    assert held == plain[:-1] + [fmt.PAGE_BREAK, 0xFFFF]
    # Round trip through a bank: the Held marker selects the waiting form.
    bank = struct.pack('<HH', 0, 0x1234)
    out = fmt.append_messages(bank, [fmt.Held(['Hi!']), ['Hi!']])
    _, entries = fmt.text_entries(out)
    codes = [[c ^ (((i + 1) * 596947 + j * 18749) & 65535) for j, c in enumerate(struct.unpack('<' + 'H' * n, d))]
             for i, (_, n, d) in enumerate(entries)]
    assert codes[0][-2:] == [fmt.PAGE_BREAK, 0xFFFF] and codes[1][-2] != fmt.PAGE_BREAK


@pytest.mark.skipif(not PARENT.is_dir(), reason='r82 parent absent')
def test_native_trainer_intros_wait_like_stock():
    from sovereign_editor import native_trainers as nt, story_authoring as sa
    from sovereign_editor.core import Project
    from sovereign_editor.formats import resource
    p = Project(PARENT)
    state = p.composed()
    trainers = sa.catalog(state, 'trainer')
    placements = [s for s in sa.catalog(state, 'sequence').values() if nt.is_native(s)]
    assert {s['trainer'] for s in placements} == {'ranger_01', 'ranger_02'}      # Brom, the twins
    tables = nt.tables(p, state, trainers, placements)
    table, text = tables[nt.TRTBL][0], tables[fmt.TEXT_ARCHIVE][nt.TRAINER_TEXT]
    stock = len(fmt.text_entries(resource(p.blob, fmt.TEXT_ARCHIVE, nt.TRAINER_TEXT)[1])[1])
    _, entries = fmt.text_entries(text)
    seen = {}
    for i in range(stock, len(entries)):
        trainer, kind = struct.unpack_from('<HH', table, 4 * i)
        _, n, data = entries[i]
        codes = [c ^ (((i + 1) * 596947 + j * 18749) & 65535) for j, c in enumerate(struct.unpack('<' + 'H' * n, data))]
        seen[(trainer, kind)] = codes[-2] == fmt.PAGE_BREAK
    intros = {k: v for k, v in seen.items() if k[1] in (0, 3, 7)}
    assert len(intros) == 3 and all(intros.values())                      # Brom 0, twins 3 and 7
    assert not any(v for k, v in seen.items() if k[1] not in (0, 3, 7))


# ---- R82-STRENGTH-01: an active Strength is acknowledged first ----------------------------------

def test_strength_boulder_acknowledges_active_strength_before_asking():
    [spec] = expand({'preset': 'field_obstacle', 'key': 'b', 'x': 5, 'z': 5, 'family': 'strength', 'persistence': 'reset'})
    spec = {**spec, 'field_flag': 0}
    code = seq.compile_sequence(spec, {}, {}, 0)
    ops = listing(code)
    names = [n for n, _ in ops]
    first = names.index('strength_flag_action')
    assert ops[first] == ('strength_flag_action', (2, 0x800C))
    # Before the move check and the yes/no question; the jump lands on the "made it possible" page.
    assert first < names.index('get_party_slot_with_move') and first < names.index('yesno')
    target = sd.decode_at(code, sd.decode_at(code, [a for a, c in sd.disassemble(code, [0]).items()
                                                   if c[1] == 'compare_var_to_value' and c[2] == [0x800C, 1]][0])[3])[4]
    assert sd.decode_at(code, target)[1] == 'npc_msg'
    msgs = seq.messages(spec['nodes'], {})
    assert sd.decode_at(code, target)[2] == [msgs.index('Strength made it possible\nto move boulders around.')]


# ---- R82-ITEM-01: pickups name the item and quantity -------------------------------------------

@pytest.mark.parametrize('count', [1, 3])
def test_pickup_announces_item_and_quantity_with_stock_text(count):
    [spec] = expand({'preset': 'pickup', 'key': 'prize', 'x': 1, 'z': 1, 'item': 50, 'count': count,
                     'persistence': 'reset'})
    assert [n['op'] for n in spec['nodes']][:3] == ['give_item', 'remove', 'found_item']
    spec = {**spec, 'field_flag': 7}
    code = seq.compile_sequence(spec, {}, {}, 0)
    ops = listing(code)
    assert ('giveitem', (50, count, 0x800A)) in ops
    assert ('callstd', (2001,)) in ops and ('wait_fanfare', ()) in ops
    assert ('setvar', (0x8004, 50)) in ops and ('setvar', (0x8005, count)) in ops
    externs = [a for n, a in ops if n == 'msgbox_extern']
    if count == 1:       # "[player] found / a Rare Candy!" (or the TM form), then the pocket line
        assert (199, 3) in externs and ('buffer_item_name_indef', (1, 0x8004)) in ops
        assert ('buffer_item_name', (1, 0x8004)) in ops
    else:                # "[player] found / 3 Rare Candies!", then the plural pocket line
        assert (199, 6) in externs and ('buffer_int', (1, 0x8005)) in ops
        assert ('buffer_item_name_plural', (2, 0x8004)) in ops and ('buffer_item_name_plural', (1, 0x8004)) in ops
    assert (199, 9) in externs and ('buffer_pocket_name', (2, 0x800A)) in ops
    # No authored "You found an item!" page remains; the object is hidden before the announcement.
    assert seq.messages(spec['nodes'], {}) == ['Your Bag is full.\nMake room and come back.']
    names = [n for n, _ in ops]
    assert names.index('hide_person') < names.index('callstd')


def test_found_item_refuses_bad_values_and_author_override_stays_plain():
    base = {'id': 'f', 'op': 'found_item', 'item': 50, 'count': 1, 'next': 'd'}
    end = {'id': 'd', 'op': 'end', 'complete': False}
    seq.validate([base, end], {}, {})
    for bad in ({'count': 0}, {'item': 0}, {'count': 100}, {'yes': 'd'}):
        with pytest.raises(EditorError):
            seq.validate([{**base, **bad}, end], {}, {})
    [spec] = expand({'preset': 'pickup', 'key': 'p', 'x': 1, 'z': 1, 'item': 50, 'texts': {'found': 'A shiny candy!'},
                     'persistence': 'reset'})
    assert spec['nodes'][2] == {'id': 'found', 'op': 'say', 'pages': ['A shiny candy!'], 'next': 'done'}


# ---- R82-REMATCH-01: one NPC for first battle, reward once and rematch ---------------------------

def test_rematch_preset_is_one_npc_with_first_win_reward_and_rematch():
    variables = {'beaten': {'flag': 0x900, 'switch': True}, 'champion': {'flag': 0x901, 'switch': True}}
    trainers = {'first': {'trainer_id': 760, 'policy': 'ordinary-v2', 'defeat_state': 'beaten', 'before': ['Hi'],
                          'after': ['Well done'], 'revisit': ['Again?'], 'battle': 'double'},
                'again': {'trainer_id': 761, 'policy': 'ordinary-v2', 'before': ['Best team!'], 'after': ['Wow'],
                          'revisit': [], 'battle': 'double'}}
    ops = presets.expand(None, {'header': 1, 'cell': [0, 0]}, {
        'preset': 'rematch', 'key': 'leader', 'x': 4, 'z': 4, 'appearance': 341, 'original': 'first', 'trainer': 'again',
        'condition': {'kind': 'badge', 'badge': 3}, 'reward': {'badge': 3}, 'reward_state': 'champion',
        'texts': {'reward': ['Take the Fog Badge!']}})
    assert [o['request']['kind'] for o in ops] == ['sequence']                  # one object, no second copy
    spec = ops[0]['request']['value']
    code = seq.compile_sequence(spec, variables, trainers, 0)
    ops_ = listing(code)
    battles = [a[0] for n, a in ops_ if n == 'trainer_battle']
    assert sorted(battles) == [760, 761]
    assert [a for n, a in ops_ if n == 'give_badge'] == [(3,)]
    # The badge is guarded by its own state (checked before give_badge, set right after).
    assert ('checkflag', (0x901,)) in ops_ and ('setflag', (0x901,)) in ops_
    # First win persists in the first team's defeat state; the rematch is behind the badge check.
    assert ('setflag', (0x900,)) in ops_ and ('check_badge', (3, 0x800C)) in ops_
    nodes = {n['id']: n for n in spec['nodes']}
    assert nodes['beaten']['yes'] == 'rewarded' and nodes['rewarded']['yes'] == 'ready'
    assert nodes['ready']['yes'] == 'offer' and nodes['offer']['yes'] == 'rematch'
    with pytest.raises(EditorError):
        presets.expand(None, {'header': 1, 'cell': [0, 0]}, {'preset': 'rematch', 'key': 'x', 'x': 1, 'z': 1,
                                                             'appearance': 1, 'original': 'a', 'trainer': 'a'})
    with pytest.raises(EditorError):
        presets.expand(None, {'header': 1, 'cell': [0, 0]}, {'preset': 'rematch', 'key': 'x', 'x': 1, 'z': 1,
                                                             'appearance': 1, 'original': 'a', 'trainer': 'b',
                                                             'reward': {'money': 5}})


# ---- R82-CAVE-01: no floating exit floor; exits revisable through a public operation -------------

CAVE_HEADER = 670
CAVE_RECTS = [[14, 21, 15, 8], [17, 3, 9, 9], [20, 12, 2, 9]]


def revise_cave(exits, **extra):
    return {'kind': 'elevation', 'context': {'header': CAVE_HEADER, 'cell': [0, 0]}, 'request': {
        'action': 'cave_room', 'rects': CAVE_RECTS, 'exits': [{'x': x, 'z': z} for x, z in exits], 'encounters': True,
        'label': 'Ridge Cave rooms (revised)', 'replaces': 'cave_room@14,21', **extra}}


def test_cave_exit_floor_version_two_stays_under_the_wall():
    from sovereign_editor import terrain_authoring as ta
    old = ta.normalise({'action': 'cave_room', 'rects': CAVE_RECTS, 'exits': [{'x': 21, 'z': 28}]}, fresh=False)
    new = ta.normalise({'action': 'cave_room', 'rects': CAVE_RECTS, 'exits': [{'x': 21, 'z': 28}]})
    assert 'version' not in old and new['version'] == 2              # recorded requests keep version 1
    with pytest.raises(EditorError):
        ta.normalise({**new, 'version': 3})


@pytest.mark.skipif(not PARENT.is_dir(), reason='r82 parent absent')
def test_cave_revision_removes_unused_exit_and_floating_floor(tmp_path):
    from sovereign_editor import terrain_authoring as ta, cave_geometry as cg, world
    from sovereign_editor.core import Project
    p = Project(PARENT).clone(tmp_path / 'p')
    before = ta.area_features(p, p.composed(), CAVE_HEADER)
    assert [(f['id'], len(f['spec']['exits']), f['spec'].get('version')) for f in before] == [('cave_room@14,21', 3, None)]
    lib = cg.library(p)
    floor = lambda spec: max(poly[:, 2].max() for name, poly in cg.pieces(lib, spec, cg.FLOOR_Y)['clipped'] if name == 'droad01')
    assert floor(before[0]['spec']) == 32                             # r82: floor strip to z 32 (void below the rim)
    # A warp still uses exit 21,28: closing it is refused; changing the rooms is refused.
    for bad in (revise_cave([(26, 28)]), {**revise_cave([(21, 28), (26, 28)]),
                                          'request': {**revise_cave([(21, 28), (26, 28)])['request'],
                                                      'rects': [[14, 21, 15, 7], [17, 3, 9, 9], [20, 12, 2, 9]]}}):
        with pytest.raises(EditorError):
            p.plan_area_edit([bad])
    p.apply_area_edit(82, operations=[revise_cave([(21, 28), (26, 28)])], label='Revise cave exits')
    after = ta.area_features(p, p.composed(), CAVE_HEADER)
    assert [(f['id'], len(f['spec']['exits']), f['spec']['version']) for f in after] == [('cave_room@14,21', 2, 2)]
    assert floor(after[0]['spec']) == 30                              # only under the hole row
    ctx = p.context(header=CAVE_HEADER, cell=[0, 0])
    pair = lambda x, z: ta.pair_at(p, p.composed(), ctx, x, z)[0]
    assert pair(16, 28) == cg.PAIRS['floor'] and pair(16, 29) == cg.PAIRS['wall']
    assert pair(21, 28) == cg.PAIRS['exit'] and pair(26, 29) == cg.PAIRS['hole']
    again = Project(p.root)                                           # replay from disk
    assert [f['spec']['exits'] for f in ta.area_features(again, again.composed(), CAVE_HEADER)] == [after[0]['spec']['exits']]
    p.undo(p.doc["revision"])
    assert [len(f['spec']['exits']) for f in ta.area_features(p, p.composed(), CAVE_HEADER)] == [3]
    assert ta.pair_at(p, p.composed(), ctx, 16, 28)[0] == cg.PAIRS['exit']


# ---- R82-TREE-01..04: blossom tree r4 geometry --------------------------------------------------

TREE = ROOT / 'work/original-content-v1/art/blossom-r4/package'


def _obj_vertices(path):
    return [tuple(map(float, l.split()[1:])) for l in path.read_text().splitlines() if l.startswith('v ')]


@pytest.mark.skipif(not TREE.is_dir(), reason='tree r4 package absent')
def test_blossom_r4_card_depth_hides_rows_north_and_beside_but_not_south():
    import json, math
    from sovereign_editor.prop_camera import PITCH, BIAS
    v = _obj_vertices(TREE / 'blossom_tree.obj')
    canopy, shadow = v[:4], v[4:]
    t = (0.0, math.sin(PITCH), math.cos(PITCH))
    depth = lambda p: 16 * (p[1] * t[1] + p[2] * t[2])
    card = [depth(p) for p in canopy]
    assert max(card) - min(card) < 1e-3                                   # camera-facing
    sprite = lambda row: 16 * math.cos(PITCH) * row + BIAS               # feet at tile centre of row
    assert abs(BIAS - 17.3) < 0.1
    assert sprite(-1) < sprite(0) < card[0] < sprite(1)                  # rows <= 0 behind, row +1 in front
    # Same screen point for the trunk base as r3 (bottom centre slid along the view axis only).
    r3 = _obj_vertices(ROOT / 'artifacts/blossom-tree-v1/source/blossom_tree.obj')
    screen = lambda p: -math.cos(PITCH) * p[1] + math.sin(PITCH) * p[2]
    base3 = [(a + b) / 2 for a, b in zip(r3[0], r3[1])]
    base4 = [(a + b) / 2 for a, b in zip(canopy[0], canopy[1])]
    assert abs(screen(base3) - screen(base4)) < 1e-6
    # Shadow: smaller than r3 and lifted to the stock h_kage height (2 units) against depth fighting.
    assert all(abs(p[1] - 2 / 16) < 1e-9 for p in shadow)
    assert max(p[0] for p in shadow) - min(p[0] for p in shadow) < 2.0
    asset = json.loads((TREE / 'asset.json').read_text())
    assert asset['collision'] == [[0, 0]]
