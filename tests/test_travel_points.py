"""Custom respawn and Fly destinations (TRAVEL-01, FIELD-05) and the boot data region. Software checks only."""
import struct
from pathlib import Path

import pytest

from sovereign_editor import character_runtime as cr, dialogue_format as fmt, resident, story_authoring
from sovereign_editor import travel_points as tp, world_authoring, world_runtime
from sovereign_editor.core import Project
from sovereign_editor.formats import EditorError

ROOT = Path(__file__).resolve().parents[1]
PARENT = ROOT / 'projects/editor-completion-v1'
pytestmark = pytest.mark.skipif(not PARENT.is_dir(), reason='r82 parent absent')
CANOPY = {'header': 542, 'cell': [2, 0]}
STATION = {'action': 'define', 'key': 'ranger_station', 'name': 'Ranger Station',
           'arrival': {'header': 542, 'x': 74, 'z': 85}, 'respawn': {'header': 667, 'x': 6, 'z': 7},
           'blackout': True, 'fly': True, 'message': ['You rested at the\nRanger Station.', 'Your Pokémon are\nhealed!']}


def edit(p, *requests):
    return p.apply_area_edit(p.doc['revision'], operations=[{'kind': 'travel', 'context': CANOPY, 'request': r}
                                                           for r in requests])


def arm_word(blob, address, size=4):
    start = struct.unpack_from('<I', blob, 0x20)[0]
    return blob[start + address - 0x02000000:start + address - 0x02000000 + size]


def patched(plan, blob, address, size):
    start = struct.unpack_from('<I', blob, 0x20)[0]
    offset = start + address - 0x02000000
    hit = [p for p in plan['patches'] if p['rom_offset'] == offset]
    assert len(hit) == 1, hex(address)
    return bytes.fromhex(hit[0]['after'])[:size]


def test_flypoint_flags_are_unused_by_every_stock_consumer():
    report = tp.qualify_flags(Project(PARENT))
    assert report['indices'] == [0x17, 0x18, 0x1A, 0x20, 0x22]
    assert all(not r['problems'] for r in report['rows'].values())


def test_respawn_and_fly_point_runtime(tmp_path):
    p = Project(PARENT).clone(tmp_path / 'p')
    edit(p, STATION)
    state = p.composed()
    point = tp.points(state)['ranger_station']
    assert (point['spawn'], point['flag']) == (31, 0x17)
    plan = world_authoring.runtime_plan(p, state)
    blob = p.blob
    boot = plan['boot_data']
    image = plan['appends'][resident.BOOT_ARCHIVE][0]
    assert boot['member'] == 17 and boot['bytes'] == len(image) <= resident.BOOT_REGION[1] - resident.BOOT_REGION[0]
    travel = plan['travel']
    at = travel['spawn_table'] - resident.BOOT_REGION[0]
    rows = [image[at + 18 * i:at + 18 * (i + 1)] for i in range(31)]
    assert rows[:30] == tp.stock_spawn_rows(blob)
    word, death, xz, fly_map, fx, fz, sp_map, sx, sz = struct.unpack('<3H6H', rows[30])
    assert (word, death, xz) == (0x17 | 1 << 8 | 1 << 9, 667, 6 | 7 << 8)
    assert (fly_map, fx, fz, sp_map, sx, sz) == (542, 74, 85, 542, 74, 85)
    # Every table literal moves by the same offset; every bound becomes 31.
    for address, offset in tp.SPAWN_LITERALS:
        assert patched(plan, blob, address, 4) == struct.pack('<I', travel['spawn_table'] + offset)
    for address, high in tp.SPAWN_BOUNDS:
        assert patched(plan, blob, address, 2) == bytes((31, high))
    # Blackout: the BL now reaches the resident routine, which calls the three stock functions.
    hook = travel['hook']
    assert patched(plan, blob, tp.BLACKOUT_QUEUE, 4) == cr.thumb_bl(tp.BLACKOUT_QUEUE, hook)
    code = plan['files'][cr.overlay(blob, 129)['file_id']][hook - 0x023D8000:hook - 0x023D8000 + 40]
    assert code == tp.hook(hook, travel['arrival_scripts'], 1)[:40]
    ids = story_authoring.allocation(p, state)
    script_id, first_text = ids['travel:ranger_station']
    at = travel['arrival_scripts'] - resident.BOOT_REGION[0]
    assert struct.unpack_from('<H', image, at)[0] == script_id
    # The respawn map's own script/text banks gain the arrival script and its two pages.
    head = p.header(667)
    replaced = story_authoring.replacements(p, state)
    scripts = fmt.script_entries(replaced[fmt.SCRIPT_ARCHIVE][head['script_file']])[1]
    assert len(scripts) == script_id
    texts = fmt.text_entries(replaced[fmt.TEXT_ARCHIVE][head['text_archive']])[1]
    assert len(texts) == first_text + 2
    body = replaced[fmt.SCRIPT_ARCHIVE][head['script_file']][scripts[-1]:]
    assert body.startswith(tp.compile_arrival(first_text, 2))
    # Boot loader: hg-engine's boot routine calls it after loading overlay 129.
    assert patched(plan, blob, resident.EXPANSION_BOOT, 4) == cr.thumb_bl(resident.EXPANSION_BOOT, boot['loader'])
    # Fly map: overlay 101 stored decompressed with 28 rows, 43 marker objects and the new table.
    info = world_runtime.town_overlay(blob)
    ov101 = plan['files'][info['file_id']]
    base = world_runtime.TOWN_ADDRESS
    for address, offset in tp.FLY_LITERALS:
        assert struct.unpack_from('<I', ov101, address - base)[0] == travel['fly_table'] + offset
    for address, high in tp.FLY_BOUNDS:
        assert ov101[address - base:address - base + 2] == bytes((28, high))
    assert ov101[tp.FLY_OBJECTS - base:tp.FLY_OBJECTS - base + 2] == bytes((43, 0x20))
    at = travel['fly_table'] - resident.BOOT_REGION[0]
    fly = image[at:at + 14 * 28]
    assert fly[:14 * 27] == b''.join(tp.stock_fly_rows(info['data']))
    assert struct.unpack('<HH9Bx', fly[14 * 27:]) == (542, 542, 0x17, 0xFF, 20, 11, 0, 0, 0x11, 0, 0)
    # Nothing else in overlay 101 changed beyond the fly-map sites (and the town-map hook tail).
    changed = [i for i in range(len(ov101)) if ov101[i] != info['data'][i]]
    sites = {a - base + k for a, _ in tp.FLY_LITERALS for k in range(4)}
    sites |= {a - base for a, _ in tp.FLY_BOUNDS} | {tp.FLY_OBJECTS - base}
    sites |= {world_runtime.TOWN_TAIL - base + k for k in range(4)}
    sites |= {tp.NAME_TAIL - base + k for k in range(4)}
    assert set(changed) <= sites
    # R101-FLY: the Fly name lookup falls back to the created town-map specs.
    assert ov101[tp.NAME_TAIL - base:tp.NAME_TAIL - base + 4] == cr.thumb_bl(tp.NAME_TAIL, travel['name_hook'])


def test_fly_selection_names_the_authored_marker_on_the_cpu(tmp_path):
    """R101-FLY (CPU, not native): selecting the authored marker passes its own map to the name
    printer (r101 read map 16384 through a NULL location spec); every stock row is unchanged."""
    import sys
    sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'work/tiana-fixes-1/python-tools')]
    import fly_qualification as fq
    from world_integration_qualification import images_from_plan
    p = Project(PARENT).clone(tmp_path / 'p')
    edit(p, STATION)
    plan = world_authoring.runtime_plan(p, p.composed())
    result = fq.qualify(p.blob, images_from_plan(p.blob, plan))
    assert result['failed'] == 0 and result['rows'] == 28
    row = result['authored'][0]
    assert (row['destination'], row['name_spec_map'], row['printed_map'], row['spawn']) == (542, 542, 542, 31)
    assert row['fly_warp'] == [542, -1, 74, 85, 1]
    assert all(r['printed_map'] == r['name_map'] for r in result['stock'] if r['destination'] and r['name_spec'] != '0x0')


def test_refusals_revise_retire_and_undo(tmp_path):
    p = Project(PARENT).clone(tmp_path / 'p')
    edit(p, STATION)
    with pytest.raises(EditorError, match='already arrives in this map'):
        edit(p, {**STATION, 'key': 'second', 'fly': False})
    with pytest.raises(EditorError, match='stock Fly destination'):
        edit(p, {**STATION, 'key': 'cherry', 'arrival': {'header': 67, 'x': 560, 'z': 395}, 'blackout': False,
                 'message': []})
    with pytest.raises(EditorError, match='not clear'):
        edit(p, {'action': 'revise', 'key': 'ranger_station', 'arrival': {'header': 542, 'x': 77, 'z': 86}})
    edit(p, {'action': 'revise', 'key': 'ranger_station', 'message': ['Welcome back!']})
    assert tp.points(p.composed())['ranger_station']['message'] == ['Welcome back!']
    edit(p, {'action': 'retire', 'key': 'ranger_station'})
    retired = tp.points(p.composed())['ranger_station']
    assert retired['retired'] and (retired['spawn'], retired['flag']) == (31, 0x17)
    row = tp.spawn_row(retired)
    assert struct.unpack_from('<H', row)[0] == 0x17        # both bits clear, data kept for saves
    reopened = Project(p.root)
    assert tp.points(reopened.composed()) == tp.points(p.composed())
    p.undo(p.doc['revision'])
    assert not tp.points(p.composed())['ranger_station']['retired']
