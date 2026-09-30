"""PROD-CAP-001 resident layout v2: 32 characters, 64 mixed trainers, 128 created headers.

Composes the real bindings on the pinned baseline and executes the candidate
overlay-129 hooks and the stock ARM9 getters under a Thumb CPU (Unicorn).
"""
import json
import struct
import sys
from pathlib import Path

import pytest

from sovereign_editor import character_runtime as cr, resident, scene_runtime, world, world_runtime as wr
from sovereign_editor.formats import EditorError, arm9_code

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / 'projects/scyther-orchestration-1/baseline.nds'
sys.path.insert(0, str(ROOT / 'work/tiana-fixes-1/python-tools'))
sys.path.insert(0, str(ROOT / 'tools'))


@pytest.fixture(scope='module')
def blob():
    if not BASELINE.exists():
        pytest.skip('Pinned baseline is local and untracked')
    return BASELINE.read_bytes()


def headers(blob, count, template=33):
    """Created records as world authoring builds them: template + private fields."""
    arm = arm9_code(blob)
    out = []
    for k in range(count):
        head = world.read_header(blob, template, arm)
        head.update(matrix=400 + k, event_file=500 + k, script_file=1100 + 2 * k, level_script=1101 + 2 * k,
                    text_archive=800 + k, wild_pokemon=143 + k if k < 100 else 255)
        head['worldmap'] = {**head['worldmap'], 'x': k % 64, 'y': (k * 7) % 64}
        out.append((world.encode_header(head), template))
    return out


def full_plan(blob, characters=32, trainers=64, count=128, collect=True):
    package = json.loads((ROOT / 'tests/fixtures/tiana.character.json').read_text())
    practice = [i % 3 == 0 for i in range(trainers)]
    layout = resident.Layout(blob)
    plan = cr.bindings(blob, [package] * characters, trainers, practice=practice, layout=layout)
    if collect:
        plan = scene_runtime.bindings(blob, plan, layout=layout)
    plan = wr.bindings(blob, plan, headers(blob, count), layout=layout)
    return resident.finish(blob, plan, layout), layout, practice


def candidate(blob, plan):
    """ROM bytes with the plan's ARM9 patches and overlay 129 (no archive appends)."""
    data = bytearray(blob)
    for p in plan['patches']:
        before, after = bytes.fromhex(p['before']), bytes.fromhex(p['after'])
        assert data[p['rom_offset']:p['rom_offset'] + len(before)] == before
        data[p['rom_offset']:p['rom_offset'] + len(after)] = after
    return bytes(data)


def test_full_load_fits_the_reservation_with_a_consolidated_report(blob):
    plan, layout, _ = full_plan(blob)
    info = cr.overlay(blob, 129)
    ext = plan['files'][info['file_id']]
    assert len(ext) <= resident.RESERVATION
    report = plan['resident']
    items = sorted(report['items'], key=lambda i: i['address'])
    for a, b in zip(items, items[1:]):
        assert a['address'] + a['size'] <= b['address'], (a['name'], b['name'])
    names = {i['name'] for i in items}
    assert {'character.gender', 'character.prize-money', 'character.partner-back', 'trainer.policy',
            'trainer.policy-table', 'scene.dispatch', 'world.header-hook', 'world.header-slot',
            'world.header-records'} <= names
    assert all(i['reach_ok'] for i in items if i.get('called_from'))
    assert report['free_bytes'] >= 0 and report['version'] == 2
    # Nothing but the two reclaimed stock tables and the tail changes in the stock image.
    stock = info['data']
    changed = [i for i in range(len(stock)) if ext[i] != stock[i]]
    allowed = [(0x419c, 0x419e)] + [(s['start'], s['end']) for s in report['segments'] if s['kind'] == 'reclaimed']
    assert all(any(lo <= i < hi for lo, hi in allowed) for i in changed)


def test_v2_refuses_more_than_the_editor_header_bound(blob):
    # Editor v1 raised the bound from 128 to 152 (count comes from a literal; resident space binds).
    layout = resident.Layout(blob)
    plan = cr.bindings(blob, [], 0)
    with pytest.raises(EditorError, match=str(wr.MAX_HEADERS)):
        wr.bindings(blob, plan, headers(blob, wr.MAX_HEADERS + 1), layout=layout)


def test_every_getter_reads_every_created_record(blob):
    pytest.importorskip('unicorn')
    import world_qualification as wq
    plan, _, _ = full_plan(blob)
    rom = candidate(blob, plan)
    info = cr.overlay(blob, 129)
    ext = plan['files'][info['file_id']]
    records = [raw for raw, _ in headers(blob, 128)]
    result = wq.cpu_parts(arm9_code(rom), ext, records, arm9_code(blob),
                          plan.get('appends', {}).get(resident.BOOT_ARCHIVE, [None])[0])
    assert result['failed'] == 0 and result['created'] == 128 and result['getters'] == 27


def test_trainer_policy_selects_practice_by_slot_including_31_32_63(blob):
    pytest.importorskip('unicorn')
    from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB
    from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3, UC_ARM_REG_R4, \
        UC_ARM_REG_R7, UC_ARM_REG_LR, UC_ARM_REG_SP
    plan, _, practice = full_plan(blob)
    info = cr.overlay(blob, 129)
    u = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
    u.mem_map(0x02000000, 0x400000)
    u.mem_write(info['address'], plan['files'][info['file_id']])
    patch = next(p for p in plan['patches'] if p['kind'] == 'trainer.practice-return')
    lo, hi = struct.unpack('<2H', bytes.fromhex(patch['after']))
    target = 0x020513ac + 4 + ((((lo & 0x7ff) << 12) | ((hi & 0x7ff) << 1)) ^ (1 << 22)) - (1 << 22)
    got = {}
    for tid in (0, 737, 738, 738 + 31, 738 + 32, 738 + 63, 738 + 64, 0xffffffff):
        u.reg_write(UC_ARM_REG_R7, tid); u.reg_write(UC_ARM_REG_R4, 1)
        u.reg_write(UC_ARM_REG_R2, 0x1234); u.reg_write(UC_ARM_REG_R3, 0x5678)
        u.reg_write(UC_ARM_REG_LR, 0x02001001); u.reg_write(UC_ARM_REG_SP, 0x02390000)
        u.emu_start(target | 1, 0x02001000, count=60)
        assert u.reg_read(UC_ARM_REG_R0) == 11 and u.reg_read(UC_ARM_REG_R1) == u.reg_read(UC_ARM_REG_R4)
        assert (u.reg_read(UC_ARM_REG_R2), u.reg_read(UC_ARM_REG_R3)) == (0x1234, 0x5678)
        got[tid] = bool(u.reg_read(UC_ARM_REG_R4) & 0x800)
    expected = {tid: 0 <= tid - 738 < 64 and practice[tid - 738] for tid in got}
    assert got == expected and got[738 + 63] == practice[63] and expected[738 + 63]


def test_partner_back_hook_maps_all_new_classes(blob):
    pytest.importorskip('unicorn')
    from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_HOOK_CODE
    from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_LR, UC_ARM_REG_SP, UC_ARM_REG_PC
    plan, _, _ = full_plan(blob)
    info = cr.overlay(blob, 129)
    patch = next(p for p in plan['patches'] if p['kind'] == 'character.partner-back')
    lo, hi = struct.unpack('<2H', bytes.fromhex(patch['after']))
    target = 0x02070d66 + 4 + ((((lo & 0x7ff) << 12) | ((hi & 0x7ff) << 1)) ^ (1 << 22)) - (1 << 22)
    u = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
    u.mem_map(0x02000000, 0x400000)
    u.mem_write(info['address'], plan['files'][info['file_id']])
    original = []
    u.hook_add(UC_HOOK_CODE, lambda uc, a, s, _: (original.append(uc.reg_read(UC_ARM_REG_R0)),
                                                  uc.reg_write(UC_ARM_REG_PC, 0x02001001)), begin=0x0207280c, end=0x0207280c)
    for cls, expected in ((0, None), (128, None), (129, 17), (129 + 31, 17 + 31), (129 + 32, None)):
        original.clear()
        u.reg_write(UC_ARM_REG_R0, cls); u.reg_write(UC_ARM_REG_R1, 0)
        u.reg_write(UC_ARM_REG_LR, 0x02001001); u.reg_write(UC_ARM_REG_SP, 0x02390000)
        u.emu_start(target | 1, 0x02001000, count=40)
        if expected is None:
            assert original == [cls]                      # stock classes tail-call the original
        else:
            assert not original and u.reg_read(UC_ARM_REG_R0) == expected


def test_readback_decodes_v2_records(blob):
    plan, _, _ = full_plan(blob)
    rom = candidate(blob, plan)
    ext = wr.decode_extension(_with_overlay(blob, rom, plan))
    assert ext['present'] and ext['version'] == 2 and ext['count'] == 128
    assert ext['records'] == [raw for raw, _ in headers(blob, 128)]


def _with_overlay(blob, rom, plan):
    """Place the candidate overlay-129 bytes into the ROM file slot and append the boot data
    member, as the export does (for decode)."""
    from sovereign_editor import containers
    from sovereign_editor.formats import file_span
    boot = plan.get('appends', {}).get(resident.BOOT_ARCHIVE)
    if boot:
        _, archive = file_span(rom, resident.BOOT_ARCHIVE)
        rom, _ = containers.replace_file(rom, resident.BOOT_ARCHIVE, containers.append_members(archive, boot))
    info = cr.overlay(blob, 129)
    data = plan['files'][info['file_id']]
    fat = struct.unpack_from('<I', rom, 0x48)[0]
    start, end = struct.unpack_from('<II', rom, fat + 8 * info['file_id'])
    out = bytearray(rom) + bytes(-len(rom) % 4)
    new = len(out)
    out += data
    struct.pack_into('<II', out, fat + 8 * info['file_id'], new, new + len(data))
    return bytes(out)
