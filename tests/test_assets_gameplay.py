"""Custom props and expanded gameplay (assets-gameplay v1). Software checks only."""
import json
import shutil
import struct
from pathlib import Path

import pytest

from sovereign_editor import (event_sequences, gameplay, nitro, nitro_writer as nw, props, services,
                              species as sp, species_forms as sf)
from sovereign_editor.core import Project
from sovereign_editor.formats import EditorError, resource

ROOT = Path(__file__).resolve().parents[1]
# The accepted r57 parent (read-only here; every test that writes works on a clone).
PARENT = ROOT / 'projects/terrain-authoring-v1'
TREE = ROOT / 'artifacts/blossom-tree-v1/source'
TREE_R1 = ROOT / 'artifacts/blossom-tree-v1/revisions/r1'
LANTERN = ROOT / 'artifacts/stone-lantern-v1/source'
CTX = {'header': 67, 'cell': [17, 12]}

pytestmark = pytest.mark.skipif(not PARENT.is_dir(), reason='accepted terrain-authoring-v1 (r57) fixture absent')


@pytest.fixture(scope='module')
def base():
    return Project(PARENT)


def fresh(tmp_path, name='p'):
    """A Project clone of the accepted r57 parent (prop- and demo-free)."""
    clone = Project(PARENT).clone(tmp_path / name)
    assert (clone.doc['revision'], len(clone.doc['map_edits'])) == (57, 542)
    return clone


def test_stock_dictionaries_and_tilesets_rebuild_exactly(base):
    blob = base.blob
    for setid in range(0, 30):
        raw = resource(blob, 'a/0/7/0', setid)[1]
        if len(raw) < 20:
            continue
        assert nw.extend_tileset(raw, [])[0] == raw
        at = struct.unpack_from('<I', raw, 16)[0]; t0 = raw[at:]; h = nw._tex0_header(t0)
        for off in (h['tex_dict'], h['pal_dict']):
            entries, size = nw.read_dictionary(t0, off)
            assert nw.dictionary([n for n, _ in entries], [e for _, e in entries]) == t0[off:off + size]
            for name, _ in entries:
                assert nw.lookup(t0[off:off + size], name) is not None


def test_bake_is_deterministic_and_the_contract_refuses(tmp_path):
    a, b = props.package(props.load_source(TREE)), props.package(props.load_source(TREE))
    assert a[0] == b[0] and a[1] == b[1]
    model = a[1]['baked/model.nsbmd']
    summary, prims = nitro.decode_model(model, geometry_only=True)
    assert summary['name'] == 'blossom' and summary['triangles'] == 4
    bad = tmp_path / 'bad'; shutil.copytree(TREE, bad)
    manifest = json.loads((bad / 'asset.json').read_text())
    for field, value, code in (('schema', 'x', 'INVALID_ASSET'), ('id', 'Upper Case', 'INVALID_ASSET')):
        m = dict(manifest, **{field: value}); (bad / 'asset.json').write_text(json.dumps(m))
        with pytest.raises(EditorError) as e:
            props.package(props.load_source(bad))
        assert e.value.code == code
    (bad / 'asset.json').write_text(json.dumps(manifest))
    from PIL import Image
    Image.new('RGB', (64, 64)).save(bad / 'canopy.png')
    with pytest.raises(EditorError, match='indexed'):
        props.package(props.load_source(bad))


def test_prop_lifecycle_ownership_revision_undo_and_portability(tmp_path):
    p = fresh(tmp_path)
    r0 = p.doc['revision']; before = p.path.read_bytes()
    staged = p.stage_prop_source(TREE_R1)
    plan = p.plan_prop_edit([staged['operation']])
    assert not plan['empty'] and p.path.read_bytes() == before and not (p.root / 'assets').exists()   # cancel = no write
    with pytest.raises(EditorError) as e:
        p.apply_prop_edit(r0 - 1, [staged['operation']])
    assert e.value.code == 'STALE_REVISION'
    p.apply_prop_edit(r0, [staged['operation']])
    assert props.verify_package(p, 'blossom', staged['package'])
    again = p.stage_prop_source(TREE_R1)
    assert again['unchanged'] and again['operation']['action'] == 'revise'
    with pytest.raises(EditorError) as e:
        p.plan_prop_edit([again['operation']])
    assert e.value.code == 'NO_CHANGE'
    place = {'action': 'place', 'asset': 'blossom', 'instance': 'a', 'x': 559.5, 'z': 393.5,
             'collision': [[-1, 0], [0, 0], [1, 0]], 'expected_revision': 2}
    with pytest.raises(EditorError) as e:
        p.plan_prop_edit([place])
    assert e.value.code == 'STALE_ASSET'
    place['expected_revision'] = 1
    overlap = dict(place, instance='b', x=561.5)                  # shares tiles 560 and 561? (560 shared)
    p.apply_prop_edit(p.doc['revision'], [place, overlap])
    view = p.prop_view()
    shared = [c for c in view['collision_owners'] if (c['x'], c['z']) == (560, 393)]
    assert shared and shared[0]['owners'] == ['a', 'b']
    # Terrain-blocked target refused; a later map permission edit of an owned cell refused.
    with pytest.raises(EditorError) as e:
        p.plan_prop_edit([dict(place, instance='c', x=555.5, z=391.5, collision=[[0, 0]])])
    assert e.value.code == 'BLOCKED_TILE'
    with pytest.raises(EditorError) as e:
        p.plan_area_edit([{'kind': 'map', 'context': CTX, 'request': {'permissions': [
            {'x': 560, 'z': 393, 'before': '0080', 'after': '0000'}]}}])
    assert e.value.code == 'COLLISION_CONFLICT'
    # Removing one owner keeps the shared tile blocked; removing both restores stock bytes.
    p.apply_prop_edit(p.doc['revision'], [{'action': 'remove', 'instance': 'a'}])
    cells = {(c['x'], c['z']): c['value'] for c in p.permission_cells(**CTX, x=557, z=393, width=6)['cells']}
    assert cells[(560, 393)] == '0080' and cells[(558, 393)] == '0000' and cells[(562, 393)] == '0080'
    p.apply_prop_edit(p.doc['revision'], [{'action': 'remove', 'instance': 'b'}])
    cells = {(c['x'], c['z']): c['value'] for c in p.permission_cells(**CTX, x=557, z=393, width=6)['cells']}
    assert all(v == '0000' for v in cells.values())
    # Place, duplicate, move, collision edit; Undo/Redo restore exactly.
    p.apply_prop_edit(p.doc['revision'], [place, {'action': 'duplicate', 'source': 'a', 'instance': 'd', 'x': 562.5, 'z': 406.5}])
    snap = json.dumps(p.prop_view(), sort_keys=True)
    p.apply_prop_edit(p.doc['revision'], [{'action': 'move', 'instance': 'd', 'x': 548.5, 'z': 406.5},
                                          {'action': 'collision', 'instance': 'd', 'collision': [[0, 0]]}])
    assert p.prop_view()['instances']['d']['cells'] == [[548, 406]]
    p.undo(p.doc['revision'])
    assert json.dumps(p.prop_view(), sort_keys=True) == snap
    p.redo(p.doc['revision'])
    assert p.prop_view()['instances']['d']['cells'] == [[548, 406]]
    # Revision: instances follow the asset; the texture set must stay the same.
    rev = p.stage_prop_source(TREE)
    assert rev['operation']['action'] == 'revise' and rev['affected_instances'] == ['a', 'd']
    p.apply_prop_edit(p.doc['revision'], [rev['operation']])
    view = p.prop_view()
    assert view['assets']['blossom']['revision'] == 2 and {i['revision'] for i in view['instances'].values()} == {2}
    # Portable: clone carries the packages; a missing package is refused on reopen.
    clone = p.clone(tmp_path / 'clone')
    assert clone.prop_view() == p.prop_view()
    shutil.rmtree(tmp_path / 'clone' / 'assets')
    with pytest.raises(EditorError) as e:
        Project(tmp_path / 'clone')
    assert e.value.code == 'MISSING_ASSET'


def test_export_is_deterministic_and_reads_back(tmp_path):
    import sys
    sys.path.insert(0, str(ROOT / 'tools'))
    import prop_readback
    p = fresh(tmp_path)
    for source in (TREE, LANTERN):
        p.apply_prop_edit(p.doc['revision'], [p.stage_prop_source(source)['operation']])
    p.apply_prop_edit(p.doc['revision'], [
        {'action': 'place', 'asset': 'blossom', 'instance': 't', 'x': 562.5, 'z': 406.5, 'collision': [[-1, 0], [0, 0], [1, 0]], 'expected_revision': 1},
        {'action': 'place', 'asset': 'lantern', 'instance': 'l', 'x': 556.5, 'z': 393.5, 'collision': [[0, 0]], 'expected_revision': 1}])
    one = p.export(tmp_path / 'e1', p.doc['revision'])
    two = Project(p.root).export(tmp_path / 'e2', p.doc['revision'])
    assert one['candidate_sha256'] == two['candidate_sha256']
    report = prop_readback.run(p, (tmp_path / 'e1/game.nds').read_bytes())
    assert report['failed'] == 0 and report['passed'] >= 20


def test_species_form_roster_and_references(base):
    s = sf.summary(base)
    assert s['expanded_supported'] == 424 and s['forms_supported'] == 56
    assert sf.lookup(base, 545)['supported'] and sf.lookup(base, 37, 1)['personal_index'] == 1131
    assert not sf.lookup(base, 544)['supported']                       # Victini: no battle pictures
    assert not sf.lookup(base, 500)['supported']                       # limbo slot
    with pytest.raises(EditorError):
        gameplay.ref_word(base, {'species': 3, 'form': 1})             # Mega Venusaur: battle-only
    assert gameplay.ref_word(base, {'species': 37, 'form': 1}) == 37 | 1 << 11


def test_gameplay_v3_typed_evolutions_and_refusals(base):
    state = base.composed()
    now = lambda a, m: gameplay.current(base, state, a, m)
    parts = gameplay.species_rows(base, now, 545)
    op = {'kind': 'species', 'id': 545, 'before_sha256': sp.fingerprint(parts),
          'changes': {'evolutions': [{'slot': 1, 'method': 'item', 'param': 85, 'target': {'species': 546}}]}}
    t = gameplay.plan_v3(base, state, 999, 33, [op])
    assert t['schema'] == gameplay.SCHEMA_V3
    raw = bytes.fromhex(next(c for c in t['changes'] if c['archive'] == sp.EVOLUTION)['after'])
    assert struct.unpack_from('<3H', raw, 6) == (7, 85, 546)
    for bad in ({'slot': 1, 'method': 'item', 'param': 33, 'target': {'species': 546}},     # not an evolution item
                {'slot': 1, 'method': 'trade', 'param': 0, 'target': {'species': 546}},     # unsupported method
                {'slot': 1, 'method': 'level', 'param': 20, 'target': {'species': 544}}):   # unqualified target
        with pytest.raises(EditorError):
            gameplay.plan_v3(base, state, 999, 33, [dict(op, changes={'evolutions': [bad]})])
    vulpix = gameplay.species_rows(base, now, 1131)
    form_op = {'kind': 'species', 'id': 37, 'form': 1, 'before_sha256': sp.fingerprint(vulpix),
               'changes': {'evolutions': [{'slot': 1, 'method': 'friendship_night', 'target': {'species': 38, 'form': 1}}]}}
    t = gameplay.plan_v3(base, state, 999, 33, [form_op])
    change = next(c for c in t['changes'] if c['archive'] == sp.EVOLUTION)
    assert change['member'] == 1131 and struct.unpack_from('<3H', bytes.fromhex(change['after']), 6) == (3, 0, 38 | 1 << 11)


# ---- services: a bounded interpreter of the emitted script commands --------------------

class Script:
    """Executes the opcodes the service compiler emits against a fake field state."""
    def __init__(self, code, party, money, bag, choices):
        self.code, self.pc, self.vars, self.log = code, 0, {}, []
        self.party, self.money, self.bag, self.choices = party, money, bag, list(choices)

    def h(self):
        v = struct.unpack_from('<H', self.code, self.pc)[0]; self.pc += 2; return v

    def val(self, v):
        return self.vars.get(v, 0) if v >= 0x4000 else v

    def run(self, limit=5000):
        cond = None
        for _ in range(limit):
            op = self.h()
            if op == 2:
                return self
            if op in (96, 97, 104, 49, 53, 150, 175, 349, 114, 115):
                self.log.append({349: 'party_ui', 114: 'hide_money', 115: 'update_money'}.get(op, op)); continue
            if op == 73:
                self.h(); continue
            if op in (30, 31):
                self.log.append(('set_flag' if op == 30 else 'clear_flag', self.h())); continue
            if op == 45:
                self.log.append(('msg', self.code[self.pc])); self.pc += 1; continue
            if op == 17:
                a, b = self.val(self.h()), self.h(); cond = (a > b) - (a < b); continue
            if op == 22:
                off = struct.unpack_from('<i', self.code, self.pc)[0]; self.pc += 4 + off; continue
            if op == 28:
                c = self.code[self.pc]; off = struct.unpack_from('<i', self.code, self.pc + 1)[0]; self.pc += 5
                take = {0: cond < 0, 1: cond == 0, 2: cond > 0, 3: cond <= 0, 4: cond >= 0, 5: cond != 0}[c]
                if take:
                    self.pc += off
                continue
            if op == 63:
                self.vars[self.h()] = self.choices.pop(0); continue
            if op == 112:
                var = self.h(); amount = struct.unpack_from('<I', self.code, self.pc)[0]; self.pc += 4
                self.vars[var] = int(self.money >= amount); continue
            if op == 111:
                amount = struct.unpack_from('<I', self.code, self.pc)[0]; self.pc += 4
                self.money -= amount; self.log.append(('charge', amount)); continue
            if op == 110:
                amount = struct.unpack_from('<I', self.code, self.pc)[0]; self.pc += 4; self.money += amount; continue
            if op == 113:
                self.h(); self.h(); self.log.append('show_money'); continue
            if op in (126, 128):
                item, count, var = self.h(), self.h(), self.h()
                have = self.bag.get(item, 0)
                if op == 128:
                    self.vars[var] = int(have >= count)
                else:
                    self.vars[var] = int(have >= count)
                    if have >= count:
                        self.bag[item] = have - count; self.log.append(('take', item, count))
                continue
            if op == 174:
                for _ in range(4):
                    self.h()
                continue
            if op == 332:
                self.vars[self.h()] = len(self.party); continue
            if op == 351:
                self.vars[self.h()] = self.choices.pop(0); self.log.append('selected'); continue
            if op == 354:
                slot, var = self.val(self.h()), self.h()
                mon = self.party[slot] if slot < len(self.party) else None
                self.vars[var] = 0 if mon is None or mon.get('egg') else mon['species']; continue
            if op == 676:
                slot, var = self.val(self.h()), self.h(); self.vars[var] = self.party[slot].get('form', 0); continue
            if op == 140:
                var, move, slot = self.h(), self.h(), self.val(self.h())
                self.vars[var] = int(move in self.party[slot]['moves']); continue
            if op == 466:
                var, slot = self.h(), self.val(self.h()); self.vars[var] = self.party[slot].get('relearnable', 0); continue
            if op in (467, 468):
                slot = self.val(self.h())
                if op == 468:
                    self.h()
                self.log.append(('learn_ui', slot)); continue
            if op == 469:
                self.vars[self.h()] = self.choices.pop(0); continue
            raise AssertionError(f'unexpected opcode {op} at {self.pc - 2}')
        raise AssertionError('script did not end')


def compile_service(node):
    spec = {'kind': 'npc', 'nodes': [node, {'id': 'done', 'op': 'end', 'complete': False},
                                     {'id': 'stop', 'op': 'end', 'complete': False}], 'once_state': None}
    code = event_sequences.compile_sequence(spec, {}, {}, 0)
    return code


TUTOR = {'id': 'tutor', 'op': 'tutor', 'move': 22, 'cost': {'money': 500}, 'eligible': [{'species': 545}, {'species': 37, 'form': 1}],
         'texts': {'offer': ['Learn Vine Whip for 500?']}, 'yes': 'done', 'no': 'stop'}


@pytest.mark.parametrize('case,party,money,choices,charged,learn', [
    ('success', [{'species': 545, 'moves': [33]}], 1000, [0, 0, 0], [('charge', 500)], True),
    ('cancel learn screen', [{'species': 545, 'moves': [33]}], 1000, [0, 0, 255], [], True),
    ('cancel party', [{'species': 545, 'moves': [33]}], 1000, [0, 255], [], False),
    ('decline offer', [{'species': 545, 'moves': [33]}], 1000, [1], [], False),
    ('insufficient funds', [{'species': 545, 'moves': [33]}], 499, [0], [], False),
    ('nobody eligible', [{'species': 16, 'moves': [33]}, {'species': 37, 'form': 0, 'moves': [33]}], 1000, [0], [], False),
    ('already knows, then cancel', [{'species': 545, 'moves': [22]}, {'species': 37, 'form': 1, 'moves': [33]}], 1000, [0, 0, 255], [], False),
    ('regional form eligible', [{'species': 16, 'moves': []}, {'species': 37, 'form': 1, 'moves': [33]}], 1000, [0, 1, 0], [('charge', 500)], True),
])
def test_tutor_charges_once_only_after_learning(case, party, money, choices, charged, learn):
    s = Script(compile_service(TUTOR), party, money, {}, choices).run()
    assert [e for e in s.log if isinstance(e, tuple) and e[0] == 'charge'] == charged, case
    assert any(isinstance(e, tuple) and e[0] == 'learn_ui' for e in s.log) == learn, case
    assert s.money == money - sum(a for _, a in charged)
    if case == 'insufficient funds' or case == 'nobody eligible':
        assert 'party_ui' not in s.log


RELEARNER = {'id': 'remind', 'op': 'relearner', 'cost': {'item': 93, 'count': 1}, 'eligible': [],
             'texts': {'offer': ['Remember a move for\none Heart Scale?']}, 'yes': 'done', 'no': 'stop'}


def test_relearner_item_cost_empty_list_and_retry():
    bag = {93: 1}
    none = Script(compile_service(RELEARNER), [{'species': 37, 'form': 1, 'moves': [39], 'relearnable': 0}], 0, bag, [0]).run()
    assert 'party_ui' not in none.log and bag[93] == 1
    cancel = Script(compile_service(RELEARNER), [{'species': 37, 'form': 1, 'moves': [39], 'relearnable': 3}], 0, bag, [0, 0, 255]).run()
    assert bag[93] == 1 and not [e for e in cancel.log if isinstance(e, tuple) and e[0] == 'take']
    ok = Script(compile_service(RELEARNER), [{'species': 37, 'form': 1, 'moves': [39], 'relearnable': 3}], 0, bag, [0, 0, 0]).run()
    assert bag[93] == 0 and [e for e in ok.log if isinstance(e, tuple) and e[0] == 'take'] == [('take', 93, 1)]
    poor = Script(compile_service(RELEARNER), [{'species': 37, 'form': 1, 'moves': [39], 'relearnable': 3}], 0, bag, [0]).run()
    assert 'party_ui' not in poor.log


def test_service_validation_refuses_bad_steps():
    for bad in (dict(TUTOR, cost={'money': 0}), dict(TUTOR, eligible=[{'species': 'x'}]), dict(TUTOR, texts={}),
                {k: v for k, v in TUTOR.items() if k != 'move'}):
        with pytest.raises(EditorError):
            event_sequences.validate([bad, {'id': 'done', 'op': 'end'}, {'id': 'stop', 'op': 'end'}], {}, {})


def test_default_service_texts_fit_the_message_box():
    from sovereign_editor import dialogue_format as fmt
    for op in services.TEXTS.values():
        for text in op.values():
            if text:
                fmt.encode_message(text)
