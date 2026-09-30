"""World integration v1: created-area identity, parenting, animation exclusion and decorative trees.

Runs on a private copy of the accepted r53 project (Canopy Walk h542 / Ranger Room h667, 128
created headers) — the state the world-integration successor was cloned from. Protected
projects are only copied, never opened for writing. Software evidence only; native
presentation is the user-run checklist.
"""
import copy
import json
import shutil
import struct
import sys
from pathlib import Path

import pytest

from sovereign_editor import world, world_identity as wi, world_runtime as wr, world_authoring as wa
from sovereign_editor import dialogue_format as fmt
from sovereign_editor.core import Project, atomic_json
from sovereign_editor.formats import EditorError, resource

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'projects/production-authoring-v2'
BASELINE = ROOT / 'projects/scyther-orchestration-1/baseline.nds'
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'work/tiana-fixes-1/python-tools')]
CW = {'header': 542, 'cell': [0, 0]}
RR = {'header': 667, 'cell': [0, 0]}


def op(context, **request):
    return {'kind': 'world', 'context': context, 'request': request}


def canopy(**extra):
    request = dict(action='identity', area='canopy_walk', name='Canopy Walk', popup=5,
                   music={'day': 1028, 'night': 1083}, weather=1,
                   town_map={'x': 20, 'y': 13, 'description': 'A ranger trail through the\ncanopy north of Route 29.'})
    request.update(extra)
    return op(CW, **request)


def ranger(**extra):
    request = dict(action='identity', area='ranger_room', name='Ranger Room', parent='canopy_walk',
                   music={'day': 1028, 'night': 1028}, weather=0)
    request.update(extra)
    return op(RR, **request)


COMBINED = [canopy(), ranger(), op(CW, action='animation', area='canopy_walk', static='auto'),
            op(CW, action='trees', area='canopy_walk', policy='decorative')]


@pytest.fixture(scope='module')
def workspace(tmp_path_factory):
    if not (SOURCE / 'project.json').exists():
        pytest.skip('The accepted r53 project is a private restored fixture')
    root = tmp_path_factory.mktemp('identity') / 'project'
    root.mkdir()
    shutil.copyfile(SOURCE / 'baseline.nds', root / 'baseline.nds')
    shutil.copyfile(SOURCE / 'project.json', root / 'project.json')
    p = Project(root)
    return root, copy.deepcopy(p.doc)


@pytest.fixture
def p(workspace):
    root, doc = workspace
    atomic_json(root / 'project.json', copy.deepcopy(doc))
    return Project(root)


def refused(p, operations, code):
    with pytest.raises(EditorError) as exc:
        p.plan_area_edit(operations)
    assert exc.value.code == code, (exc.value.code, str(exc.value))
    return str(exc.value)


# ---- masked header encoding (unit) ------------------------------------------------------

def test_masked_header_writes_keep_every_other_bit():
    blob = BASELINE.read_bytes()
    head = world.read_header(blob, 33)
    raw = bytearray.fromhex(head['hex'])
    raw[19] |= 0xF0                                  # mom-call bits
    struct.pack_into('<I', raw, 20, struct.unpack_from('<I', raw, 20)[0] | 0xFE000000)   # flags
    head = world.decode_header(bytes(raw), 33, head['name'], None)
    assert world.encode_header(head) == bytes(raw)
    edited = dict(head, location_name=240, area_icon=5, weather=1, kanto=True, music_night=1083)
    out = world.encode_header(edited)
    back = world.decode_header(out, 33, head['name'], None)
    assert (back['location_name'], back['area_icon'], back['weather'], back['kanto'], back['music_night']) == (240, 5, 1, True, 1083)
    changed = [i for i in range(24) if out[i] != raw[i]]
    assert set(changed) <= {14, 15, 18, 19, 20}
    assert back['mom_call_intro'] == head['mom_call_intro'] and back['flags'] == head['flags']
    for field, value in (('weather', 128), ('area_icon', 16), ('location_name', 256)):
        with pytest.raises(EditorError):
            world.encode_header(dict(head, **{field: value}))


def test_v3_overrides_are_shared_capped_and_v2_bytes_unchanged_without_them():
    blob = BASELINE.read_bytes()
    arm = world_arm = __import__('sovereign_editor.formats', fromlist=['arm9_code']).arm9_code(blob)
    template = world.read_header(blob, 33, arm)
    plain = world.encode_header(dict(template, matrix=400, event_file=500, script_file=1100, level_script=1101,
                                     text_archive=800))
    entry = wr.compact(world_arm, plain, 33, [])
    assert entry == wr.compact(world_arm, plain, 33)       # no identity: the v2 entry, spare byte 0
    named = world.encode_header(dict(world.decode_header(plain, 540, '', None), location_name=235, music_night=1083))
    with pytest.raises(EditorError):
        wr.compact(world_arm, named, 33)                   # v2 refuses identity changes
    overrides = []
    first = wr.compact(world_arm, named, 33, overrides)
    again = wr.compact(world_arm, named, 33, overrides)
    assert first == again and len(overrides) == 1 and first[13] == 1
    stock = arm[wr.TABLE - wr.ARM_BASE + 33 * 24:wr.TABLE - wr.ARM_BASE + 34 * 24]
    assert wr.materialize(stock, first, overrides) == named
    full = [bytes([k]) * wr.OVERRIDE.size for k in range(wr.MAX_OVERRIDES)]
    with pytest.raises(EditorError) as exc:
        wr.compact(world_arm, named, 33, full)
    assert exc.value.code == 'RESOURCE_CAPACITY'


# ---- Project operations ---------------------------------------------------------------

def test_combined_identity_apply_undo_redo_and_reopen(p):
    before = {h: p.header(h)['hex'] for h in (542, 667)}
    plan = p.plan_area_edit(copy.deepcopy(COMBINED), label='World integration')
    schemas = [t['schema'] for t in plan['transactions']]
    assert schemas[:3] == [wi.IDENTITY_SCHEMA, wi.IDENTITY_SCHEMA, wi.ANIMATION_SCHEMA]
    assert len(schemas) > 3 and all(s != wi.IDENTITY_SCHEMA for s in schemas[3:])
    revision = p.doc['revision']
    p.apply_area_edit(revision, operations=copy.deepcopy(COMBINED), label='World integration')
    assert p.doc['revision'] == revision + 1
    cw, rr = p.header(542), p.header(667)
    assert (cw['location_name'], cw['area_icon'], cw['music_day'], cw['music_night'], cw['weather'], cw['kanto']) \
        == (235, 5, 1028, 1083, 1, False)
    assert cw['worldmap']['x'] == 20 and cw['worldmap']['y'] == 11
    assert (rr['location_name'], rr['kanto'], rr['worldmap']['x'], rr['worldmap']['y'], rr['weather']) == (236, False, 20, 11, 0)
    rows = {r['area']: r for r in wi.view(p)['areas']}
    assert rows['canopy_walk']['location_name'] == 'Canopy Walk' and rows['canopy_walk']['static_members']
    assert rows['ranger_room']['parent'] == 'canopy_walk' and rows['ranger_room']['location_name'] == 'Ranger Room'
    assert 'canopy_walk' not in wi.headbutt_tiles(p, p.composed())
    after = {h: p.header(h)['hex'] for h in (542, 667)}
    p.undo(p.doc['revision'])
    assert {h: p.header(h)['hex'] for h in (542, 667)} == before and not wi.identities(p.composed())
    p.redo(p.doc['revision'])
    assert {h: p.header(h)['hex'] for h in (542, 667)} == after
    fresh = Project(p.root)
    assert {h: fresh.header(h)['hex'] for h in (542, 667)} == after
    assert wi.static_list(fresh.composed()) == wi.static_list(p.composed())


def test_no_op_stale_revision_and_tamper_refuse(p):
    p.apply_area_edit(p.doc['revision'], operations=[canopy()])
    refused(p, [canopy()], 'NO_CHANGE')
    with pytest.raises(EditorError):
        p.apply_area_edit(p.doc['revision'] - 1, operations=[ranger()])
    doc = json.loads(p.path.read_text())
    t = doc['map_edits'][-1]
    assert t['schema'] == wi.IDENTITY_SCHEMA
    raw = bytearray.fromhex(t['after'])
    raw[20] ^= 0x02                                   # weather bit
    t['after'] = raw.hex()
    atomic_json(p.path, doc)
    with pytest.raises(EditorError):
        Project(p.root)


def test_identity_refusals_before_write(p):
    refused(p, [canopy(colour='green')], 'INVALID_INPUT')
    refused(p, [canopy(name='Route 29')], 'DUPLICATE_NAME')
    refused(p, [canopy(name='A' * 17)], 'INVALID_INPUT')
    refused(p, [canopy(name=' Canopy')], 'INVALID_INPUT')
    refused(p, [canopy(music={'day': 1, 'night': 1083})], 'UNSUPPORTED_MUSIC')
    refused(p, [canopy(music={'day': 1028})], 'INVALID_INPUT')
    refused(p, [canopy(weather=2)], 'UNSUPPORTED_WEATHER')
    refused(p, [canopy(popup=10)], 'INVALID_INPUT')
    refused(p, [canopy(town_map={'x': 18, 'y': 14})], 'POSITION_CONFLICT')          # Route 29's own cell
    refused(p, [canopy(town_map={'x': 0, 'y': 13})], 'UNSUPPORTED_POSITION')
    refused(p, [canopy(town_map={'x': 20, 'y': 18})], 'UNSUPPORTED_POSITION')
    covered = wi.qualified(p)['town_covered']
    indigo = next((28, y) for y in (6, 9, 10, 11, 12) if (28, y) not in covered)
    refused(p, [canopy(town_map={'x': indigo[0], 'y': indigo[1]})], 'UNSUPPORTED_POSITION')   # Indigo Plateau
    refused(p, [canopy(town_map={'x': 28, 'y': 10})], 'POSITION_CONFLICT' if (28, 10) in covered else 'UNSUPPORTED_POSITION')
    refused(p, [canopy(region='kanto')], 'INVALID_REGION')
    refused(p, [canopy(parent='ranger_room')], 'INVALID_PARENT')                    # outdoor never parented
    refused(p, [ranger()], 'INVALID_PARENT')                                        # parent has no marker yet
    refused(p, [canopy(), ranger(parent='ranger_room')], 'INVALID_INPUT')           # self
    refused(p, [canopy(), ranger(parent='nowhere')], 'NOT_FOUND')
    refused(p, [canopy(), ranger(town_map={'x': 21, 'y': 13})], 'INVALID_INPUT')
    refused(p, [canopy(), ranger(share_name=True)], 'INVALID_INPUT')                # shares and names
    refused(p, [op(CW, action='identity', area='ranger_room', weather=0, music={'day': 1028, 'night': 1028})],
            'CONTEXT_MISMATCH')
    refused(p, [op(CW, action='identity', area='no_such_area', weather=0)], 'NOT_FOUND')
    refused(p, [op(CW, action='trees', area='canopy_walk', policy='decorative', retain=[{'x': 0, 'z': 0}])],
            'UNSUPPORTED_HEADBUTT')
    refused(p, [op(CW, action='trees', area='canopy_walk', policy='interactable')], 'INVALID_INPUT')


def test_parent_follows_marker_and_shared_name_in_one_batch(p):
    p.apply_area_edit(p.doc['revision'], operations=[canopy(), ranger(name=None, share_name=True)])
    assert p.header(667)['location_name'] == p.header(542)['location_name'] == 235
    plan = p.plan_area_edit([canopy(town_map={'x': 21, 'y': 13})])
    follow = [t for t in plan['transactions'] if t.get('area') == 'ranger_room']
    assert len(follow) == 1 and follow[0]['label'] == 'Follow parent: ranger_room'
    p.apply_area_edit(p.doc['revision'], operations=[canopy(town_map={'x': 21, 'y': 13})])
    assert p.header(667)['worldmap']['x'] == p.header(542)['worldmap']['x'] == 21
    # Renaming the parent renames the shared map section in place; the interior keeps sharing it.
    p.apply_area_edit(p.doc['revision'], operations=[canopy(name='Canopy Trail')])
    assert p.header(667)['location_name'] == 235
    assert wi.sections(p.composed())[235]['name'] == 'Canopy Trail'
    # Clearing the parent's marker cannot leave its interior pointing at a stale position.
    refused(p, [canopy(town_map=False)], 'INVALID_PARENT')


def test_location_name_capacity_is_the_8_bit_map_section(p):
    q = wi.qualified(p)
    view = wi.view(p)
    assert view['capacity']['location_names'] == {**view['capacity']['location_names'], 'used': 0,
                                                  'limit': 256 - q['section_base'], 'first_id': q['section_base']}
    state = copy.deepcopy(p.composed())
    known = {q['section_base'] + k: {'name': f'Place {k}', 'owner': f'x{k}'} for k in range(256 - q['section_base'])}
    state['world']['sections'] = known
    ctx = p.context(**CW)
    with pytest.raises(EditorError) as exc:
        wi.plan_identity(p, ctx, state, len(p.doc['map_edits']), area='canopy_walk', name='One Too Many')
    assert exc.value.code == 'RESOURCE_CAPACITY'
    # The last free map section (255) is still allocated and reads back from the appended bank.
    del state['world']['sections'][255]
    t = wi.plan_identity(p, ctx, state, len(p.doc['map_edits']), area='canopy_walk', name='Last Place')
    assert t['section']['id'] == 255 and t['identity']['section'] == 255
    state['world']['sections'][255] = {'name': 'Last Place', 'owner': 'canopy_walk'}
    bank = wi.replacements(p, state)[fmt.TEXT_ARCHIVE][wi.SECTION_TEXT]
    assert len(fmt.text_entries(bank)[1]) == 256 and wi._stock_names(bank)[255] == 'Last Place'


def test_legacy_creation_transactions_replay_unchanged(p):
    schemas = [t['schema'] for t in p.doc['map_edits'] if t.get('schema') in wa.AREA_SCHEMAS]
    assert schemas and wa.SCHEMA_V3 not in schemas          # accepted r53 creations stay v1/v2
    assert not wi.static_list(p.composed()) and not wi.identities(p.composed())


# ---- export and independent readback -----------------------------------------------------

def test_export_reads_back_identity_text_static_members_and_town_map(p, tmp_path):
    import world_integration_qualification as wiq
    p.apply_area_edit(p.doc['revision'], operations=copy.deepcopy(COMBINED))
    p.export(tmp_path / 'first', p.doc['revision'])
    rom = (tmp_path / 'first/game.nds').read_bytes()
    report = wiq.readback(rom, p)
    assert report['failed'] == 0, [c for c in report['checks'] if not c['pass']]
    cpu = wiq.run(rom, p.blob)
    assert cpu['failed'] == 0 and cpu['town']['created_hits'] >= 1
    assert cpu['static']['excluded_created'] == wi.static_list(p.composed())
    p.export(tmp_path / 'second', p.doc['revision'])
    assert (tmp_path / 'second/game.nds').read_bytes() == rom


# ---- native editor ------------------------------------------------------------------------

def test_world_editor_identity_tab_previews_and_applies_the_shared_operation(p, tmp_path):
    import os
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtWidgets import QApplication
    from sovereign_editor.map_inspector import MapInspectorWindow
    from sovereign_editor.world_ui import WorldEditor
    app = QApplication.instance() or QApplication([])
    w = MapInspectorWindow(p, context=(542, [0, 0])); w.show(); app.processEvents()
    d = WorldEditor(w); d.tabs.setCurrentIndex(d.tabs.indexOf(d.id_name.parentWidget())); d.show(); app.processEvents()
    assert d.identity_key == 'canopy_walk' and 'Route 15' in d.id_area.text()
    before = p.path.read_bytes()
    d.id_name.setText('Canopy Walk'); d.id_popup.setCurrentIndex(d.id_popup.findData(5))
    d.id_day.setCurrentIndex(d.id_day.findData(1028)); d.id_night.setCurrentIndex(d.id_night.findData(1083))
    d.id_weather.setCurrentIndex(d.id_weather.findData(1))
    d.id_marker.setCurrentIndex(d.id_marker.findData('set')); d.id_x.setValue(18); d.id_y.setValue(14)
    d.stage_identity(); app.processEvents()
    assert 'POSITION_CONFLICT' in d.status.text() and not d.apply_button.isEnabled()
    d.id_x.setValue(20); d.id_y.setValue(13)
    d.stage_identity(); app.processEvents()
    text = d.summary.toPlainText()
    assert 'Identity canopy_walk' in text and 'music_night: 1060 → 1083' in text and d.apply_button.isEnabled()
    assert p.path.read_bytes() == before
    d.grab().save(str(tmp_path / 'identity-tab.png'))
    d.apply(); app.processEvents()
    assert p.header(542)['location_name'] == 235 and 'Canopy Walk' in d.id_area.text()
    d.stage_trees(); app.processEvents()
    assert d.apply_button.isEnabled() and 'permission tile' in d.summary.toPlainText()
    d.close(); w.close(); app.processEvents()
