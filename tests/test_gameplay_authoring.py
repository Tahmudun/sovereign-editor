"""Gameplay authoring v1: species records, multi-area batches and legacy composition."""
import copy
import json
import struct
import sys
from pathlib import Path

import ndspy.narc
import ndspy.rom
import pytest

from sovereign_editor import gameplay as g, species as sp
from sovereign_editor.core import Project, atomic_json
from sovereign_editor.formats import EditorError, digest

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT/'projects/scyther-orchestration-1/baseline.nds'


@pytest.fixture(scope='module')
def workspace(tmp_path_factory):
    if not BASELINE.exists():
        pytest.skip('Pinned baseline is local and untracked')
    p = Project.create(BASELINE, tmp_path_factory.mktemp('authoring')/'project')
    return p.root, copy.deepcopy(p.doc)


@pytest.fixture
def p(workspace):
    root, doc = workspace; atomic_json(root/'project.json', copy.deepcopy(doc))
    return Project(root)


def species_ops(p, learn=True):
    s, f, pid = (p.gameplay_species(i) for i in (161, 162, 16))
    sentret = {'stats': {'hp': 40, 'defense': 38, 'speed': 25}, 'types': [0, 4], 'abilities': [51, 50], 'growth': 4,
               'evolutions': [{'slot': 0, 'level': 6, 'target': 162}], 'machines': [{'machine': 16, 'compatible': True}]}
    if learn:
        sentret['learnset'] = {'add': [{'level': 5, 'move': 98}]}
    return [{'kind': 'species', 'id': 161, 'before_sha256': s['before_sha256'], 'changes': sentret},
            {'kind': 'species', 'id': 162, 'before_sha256': f['before_sha256'], 'changes': {'types': [0, 4], 'growth': 4}},
            {'kind': 'species', 'id': 16, 'before_sha256': pid['before_sha256'],
             'changes': {'stats': {'speed': 60}, 'machines': [{'machine': 16, 'compatible': False}]}}]


def encounter_ops(p):
    r29, r30 = p.gameplay_data(33)['encounters'], p.gameplay_data(34)['encounters']
    return [{'kind': 'encounters', 'header': 33, 'before_sha256': r29['before_sha256'], 'edits': [
                {'method': 'grass', 'field': 'level', 'slot': 0, 'value': 3},
                {'method': 'grass', 'field': 'species', 'time': 'night', 'slot': 0, 'value': 16}]},
            {'kind': 'encounters', 'header': 34, 'before_sha256': r30['before_sha256'], 'edits': [
                {'method': 'grass', 'field': 'level', 'slot': 1, 'value': 4}]}]


def test_catalogs_and_species_view_use_actual_build_data(p):
    types = p.gameplay_catalog('types', limit=100)['entries']
    assert len(types) == 18 and types[9]['name'] == 'Fairy' and types[17]['name'] == 'Dark'
    assert p.gameplay_catalog('abilities', limit=100)['total'] == 123
    assert [r['name'] for r in p.gameplay_catalog('growth')['entries']][4] == 'Fast'
    tm = p.gameplay_catalog('machines', search='TM017')['entries']
    assert tm == [{'id': 16, 'name': 'TM017', 'item': 344, 'move': 182, 'supported': True, 'reason': ''}]
    v = p.gameplay_species(161)
    assert v['personal'] == {'stats': {'hp': 35, 'attack': 46, 'defense': 34, 'speed': 20, 'sp_attack': 35, 'sp_defense': 45},
                             'types': [0, 0], 'growth': 0, 'abilities': [50, 51]}
    assert v['learnset'][:3] == [{'level': 1, 'move': 10}, {'level': 4, 'move': 111}, {'level': 7, 'move': 98}]
    assert [e for e in v['evolution'] if e['method']] == [{'slot': 0, 'method': 4, 'param': 15, 'target': 162, 'form': 0, 'editable': True}]
    assert 16 in v['machines']['base_compatible'] and all(v['editable'].values())
    with pytest.raises(EditorError):
        p.gameplay_species(494)


def test_encoders_preserve_unselected_bytes_entries_methods_and_bits(p):
    parts = sp.members(p, p.composed(), 161)
    # An expanded move in an unselected entry and a non-level method survive edits.
    words = list(struct.unpack('<34I', parts['learnset'])); words.insert(2, 3 << 16 | 900); words.pop()
    parts['learnset'] = struct.pack('<34I', *words)
    evo = bytearray(parts['evolution']); struct.pack_into('<3H', evo, 6, 7, 81, 162 | 1 << 11); parts['evolution'] = bytes(evo)
    machines = bytearray(parts['machines']); machines[40] |= 0x80; parts['machines'] = bytes(machines)
    new, notes = sp.encode(p, parts, 161, {'stats': {'speed': 25}, 'abilities': [51, 50],
                                           'learnset': {'add': [{'level': 5, 'move': 98}]},
                                           'evolutions': [{'slot': 0, 'level': 6, 'target': 162}],
                                           'machines': [{'machine': 16, 'compatible': False}]})
    old = parts['personal']; raw = new['personal']
    assert raw[3] == 25 and struct.unpack_from('<H', raw, 22)[0] == 51 and struct.unpack_from('<H', raw, 26)[0] == 50
    assert [i for i in range(44) if raw[i] != old[i]] == [3, 22, 26]
    rows = sp.decode_learnset(new['learnset'])
    assert {'level': 3, 'move': 900} in rows and rows[rows.index({'level': 5, 'move': 98}) - 1]['level'] <= 5
    assert len(rows) == len(sp.decode_learnset(parts['learnset'])) + 1
    assert new['evolution'][6:12] == parts['evolution'][6:12] and struct.unpack_from('<3H', new['evolution'], 0) == (4, 6, 162)
    assert new['machines'][40] & 0x80 and not struct.unpack('<11I', new['machines'])[0] >> 16 & 1
    assert [i for i in range(44) if new['machines'][i] != parts['machines'][i]] == [2]
    assert any('battle effect' in n for n in notes)
    with pytest.raises(EditorError, match='level-evolution'):
        sp.encode(p, parts, 161, {'evolutions': [{'slot': 1, 'level': 6, 'target': 162}]})
    with pytest.raises(EditorError, match='nine-entry'):
        sp.decode_evolutions(parts['evolution'] + b'\0' * 6)


@pytest.mark.parametrize('changes,code', [
    ({'stats': {'hp': 0}}, 'INVALID_INPUT'), ({'stats': {'luck': 5}}, 'INVALID_INPUT'),
    ({'types': [0, 18]}, 'INVALID_INPUT'), ({'types': [0]}, 'INVALID_INPUT'),
    ({'abilities': [124, 0]}, 'INVALID_INPUT'), ({'abilities': [0, 50]}, 'INVALID_INPUT'), ({'growth': 6}, 'INVALID_INPUT'),
    ({'learnset': {'add': [{'level': 101, 'move': 98}]}}, 'INVALID_INPUT'),
    ({'learnset': {'add': [{'level': 5, 'move': 900}]}}, 'UNSUPPORTED_ID'),
    ({'learnset': {'add': [{'level': 1, 'move': 10}]}}, 'DUPLICATE_EDIT'),
    ({'learnset': {'remove': [{'level': 2, 'move': 10}]}}, 'BEFORE_VALUE_MISMATCH'),
    ({'evolutions': [{'slot': 0, 'level': 6, 'target': 161}]}, 'INVALID_INPUT'),
    ({'evolutions': [{'slot': 0, 'level': 6, 'target': 600}]}, 'UNSUPPORTED_ID'),
    ({'machines': [{'machine': 100, 'compatible': True}]}, 'INVALID_INPUT'),
    ({'machines': [{'machine': 16, 'compatible': 1}]}, 'INVALID_INPUT'), ({'shiny': True}, 'INVALID_INPUT')])
def test_species_refusals(p, changes, code):
    with pytest.raises(EditorError) as exc:
        sp.encode(p, sp.members(p, p.composed(), 161), 161, changes)
    assert exc.value.code == code


def test_learnset_capacity_is_enforced(p):
    parts = sp.members(p, p.composed(), 475)
    assert len(sp.decode_learnset(parts['learnset'])) == 33
    with pytest.raises(EditorError, match='capacity'):
        sp.encode(p, parts, 475, {'learnset': {'add': [{'level': 0, 'move': 33}]}})
    removed = sp.decode_learnset(parts['learnset'])[5]
    new, _ = sp.encode(p, parts, 475, {'learnset': {'remove': [removed], 'add': [{'level': 0, 'move': 33}]}})
    rows = sp.decode_learnset(new['learnset']); at = rows.index({'level': 0, 'move': 33})
    # Stable insertion: after the existing level-0 (evolution) entries, before level 1.
    assert len(rows) == 33 and all(r['level'] == 0 for r in rows[:at]) and rows[at + 1]['level'] >= 0


def test_multi_area_batch_atomic_noop_stale_reopen_undo_redo(p):
    before = p.path.read_bytes(); ops = species_ops(p) + encounter_ops(p)
    plan = p.plan_gameplay_edit(ops, header=33); t = plan['transaction']
    assert p.path.read_bytes() == before and t['schema'] == g.SCHEMA_V3   # new edits are v3 (assets-gameplay v1)
    assert sorted({(c['archive'], c.get('offset')) for c in t['changes']}) == sorted({
        ('a/0/0/2', None), ('a/0/3/4', None), ('a/0/3/3', 161*136), ('a/0/2/8', 16*44), ('a/0/3/7', None)})
    assert t['dependencies']['headers'].keys() == {'33', '34'}
    impact = t['impact']
    # v3 impact rows also list exact {species, form} refs; the v2 fields are unchanged.
    assert {'member': 1, 'headers': [33], 'species': [16, 161]} in [
        {k: e[k] for k in ('member', 'headers', 'species')} for e in impact['encounter_files']]
    assert any(r['id'] == 47 for r in impact['trainers']) and impact['evolution_sources']
    bad = copy.deepcopy(ops); bad[-1]['edits'][0]['value'] = 101
    with pytest.raises(EditorError):
        p.apply_gameplay_edit(0, operations=bad, header=33)
    assert p.path.read_bytes() == before
    p.apply_gameplay_edit(0, operations=ops, header=33); assert p.doc['revision'] == 1
    reopened = Project(p.root)
    assert reopened.gameplay_species(161)['personal']['types'] == [0, 4]
    assert reopened.gameplay_species(161)['learnset'][2] == {'level': 5, 'move': 98}
    assert reopened.gameplay_data(33)['encounters']['methods']['grass']['night'][0] == 16
    same = p.path.read_bytes()
    assert not p.apply_gameplay_edit(1, operations=species_ops(p, learn=False)[1:], header=33)['changed']
    assert p.path.read_bytes() == same
    with pytest.raises(EditorError, match='revision'):
        p.apply_gameplay_edit(0, operations=species_ops(p, learn=False), header=33)
    with pytest.raises(EditorError, match='before-value'):
        p.plan_gameplay_edit(ops, header=33)
    for field in ('dependencies', 'changes', 'index', 'impact'):
        doc = copy.deepcopy(p.doc); last = doc['map_edits'][-1]
        if field == 'dependencies': last[field]['headers']['33'] = '00'
        elif field == 'changes': last[field][0]['after'] = '00'
        elif field == 'impact': last[field]['trainers'] = []
        else: last['index'] = 5
        with pytest.raises(EditorError):
            p._validate_state(doc)
    p.undo(1); assert p.doc['map_edits'] == [] and p.gameplay_species(161)['personal']['types'] == [0, 0]
    p.redo(2); assert p.gameplay_species(161)['personal']['types'] == [0, 4]


def test_duplicate_targets_in_one_batch_refuse(p):
    ops = encounter_ops(p); ops.append(copy.deepcopy(ops[1]))
    with pytest.raises(EditorError, match='Duplicate'):
        p.plan_gameplay_edit(ops, header=33)
    ops = species_ops(p); ops.append(copy.deepcopy(ops[0]))
    with pytest.raises(EditorError, match='Duplicate'):
        p.plan_gameplay_edit(ops, header=33)


def test_areas_discovery_reports_resources_and_reasons(p):
    rows = {r['header']: r for r in p.gameplay_areas(search='R', limit=400)['areas']}
    assert rows[33]['encounters'] and rows[33]['trainers'] == [] and 'No stock trainer' in rows[33]['trainer_reason']
    assert rows[34]['trainers'] == [8, 47, 249] and rows[34]['wild_member'] == 3
    assert not rows[134]['encounters'] and 'no wild encounter table' in rows[134]['encounter_reason']
    assert p.gameplay_data(33)['route'] == 'R29' and p.gameplay_data(33)['trainers'] == []


def test_legacy_v1_history_replays_and_composes_with_later_event_edits(p):
    """r37-style v1 transactions keep their meaning; later compatible edits no longer invalidate them."""
    d = p.gameplay_data(34); t47 = next(t for t in d['trainers'] if t['id'] == 47)
    v1 = g.plan_v1(p, p.composed(), 0, 34, [{'kind': 'trainer', 'id': 47, 'before_sha256': t47['before_sha256'],
         'party': [{'species': 16, 'level': 4, 'held_item': 0, 'moves': [16, 33, 28, 0]}]}])
    assert v1['schema'] == g.SCHEMA and 'event' in v1['dependencies']
    with p._locked(0):
        p._commit('map.transaction', {'map_edits': [v1]})
    p.apply_area_edit(1, operations=[{'kind': 'event', 'context': {'header': 34, 'cell': [17, 10]},
                                      'request': {'kind': 'npc', 'event_id': 5, 'values': {'x': 549, 'z': 348}}}])
    ops = species_ops(p, learn=False)[2:]
    p.apply_gameplay_edit(2, operations=ops, header=34)
    reopened = Project(p.root)
    assert [t['schema'] for t in reopened.doc['map_edits']][0::2] == [g.SCHEMA, g.SCHEMA_V3]
    assert next(t for t in reopened.gameplay_data(34)['trainers'] if t['id'] == 47)['position'] == [549, 348]
    state = copy.deepcopy(reopened.composed()); state['gameplay_checks'][0]['headers']['34'] = '00'
    with pytest.raises(EditorError, match='changed'):
        g.validate(reopened, state)
    doc = copy.deepcopy(reopened.doc); doc['map_edits'][0]['dependencies']['event'] = 'bad'
    with pytest.raises(EditorError):
        reopened._validate_state(doc)


def test_independent_export_readback_rows_and_repeat(p, tmp_path):
    p.apply_gameplay_edit(0, operations=species_ops(p) + encounter_ops(p), header=33)
    p.export(tmp_path/'first', 1)
    raw = (tmp_path/'first/game.nds').read_bytes(); rom = ndspy.rom.NintendoDSRom(raw); base = ndspy.rom.NintendoDSRom(p.blob)
    def members(r, path): return ndspy.narc.NARC(r.getFileByName(path)).files
    for path, expected in [('a/0/0/2', [16, 161, 162]), ('a/0/3/4', [161]), ('a/0/3/7', [1, 3]), ('a/0/3/3', [0]), ('a/0/2/8', [14])]:
        a, b = members(base, path), members(rom, path)
        assert len(a) == len(b) and [i for i, (x, y) in enumerate(zip(a, b)) if x != y] == expected
    table_a, table_b = members(base, 'a/0/3/3')[0], members(rom, 'a/0/3/3')[0]
    assert [i // 136 for i in range(0, len(table_a), 136) if table_a[i:i+136] != table_b[i:i+136]] == [161]
    mach_a, mach_b = members(base, 'a/0/2/8')[14], members(rom, 'a/0/2/8')[14]
    assert [i // 44 for i in range(0, len(mach_a), 44) if mach_a[i:i+44] != mach_b[i:i+44]] == [16]
    assert all(members(base, 'a/0/2/8')[i] == members(rom, 'a/0/2/8')[i] for i in range(len(members(base, 'a/0/2/8'))) if i != 14)
    personal = members(rom, 'a/0/0/2')[161]
    assert list(personal[:8]) == [40, 46, 38, 25, 35, 45, 0, 4] and personal[19] == 4 and personal[28:] == members(base, 'a/0/0/2')[161][28:]
    assert rom.arm9 == base.arm9 and rom.arm9OverlayTable == base.arm9OverlayTable
    p.export(tmp_path/'repeat', 1); assert (tmp_path/'repeat/game.nds').read_bytes() == raw


def test_cli_species_areas_and_edit_share_project(p, tmp_path, capsys):
    from sovereign_editor.cli import main
    assert main(['gameplay-species', '--project', str(p.root), '--species', '161']) == 0
    assert json.loads(capsys.readouterr().out)['result'] == p.gameplay_species(161)
    assert main(['gameplay-areas', '--project', str(p.root), '--search', 'R30', '--limit', '2']) == 0
    assert json.loads(capsys.readouterr().out)['result']['areas'][0]['header'] == 34
    assert main(['gameplay-catalog', '--project', str(p.root), '--kind', 'machines', '--search', 'HM08']) == 0
    assert json.loads(capsys.readouterr().out)['result']['entries'][0]['move'] == 431
    q = tmp_path/'request.json'; q.write_text(json.dumps({'header': 33, 'operations': species_ops(p)}))
    before = p.path.read_bytes()
    assert main(['gameplay-edit', '--project', str(p.root), '--request', str(q), '--dry-run']) == 0
    result = json.loads(capsys.readouterr().out)['result']
    assert result['impact']['evolution_sources'] and p.path.read_bytes() == before
    assert main(['gameplay-edit', '--project', str(p.root), '--request', str(q), '--revision', '0']) == 0
    capsys.readouterr(); assert Project(p.root).gameplay_species(16)['personal']['stats']['speed'] == 60


def test_consumers_execute_candidate_records(p):
    sys.path[:0] = [str(ROOT/'work/tiana-fixes-1/python-tools'), str(ROOT/'work/tiana-events-1/python-tools'), str(ROOT/'tools')]
    pytest.importorskip('unicorn'); pytest.importorskip('elftools')
    import gameplay_authoring_qualification as q
    if not q.ENGINE.exists():
        pytest.skip('Engine build outputs are local')
    p.apply_gameplay_edit(0, operations=species_ops(p), header=33)
    report = q.run(p.blob, q.candidate_from_project(p))
    assert report['failed'] == 0, [c for c in report['checks'] if not c['pass']]
    outcome = next(c for c in report['checks'] if c['check'].startswith('overlay 134'))['detail']
    assert outcome['161@5'] == 0 and outcome['161@6'] == 162 and outcome['16@18'] == 17
    compat = {c['check'].split()[1]: c['detail'] for c in report['checks'] if 'TM017 compatibility' in c['check']}
    assert compat == {'161': 1, '162': 1, '16': 0}
