"""WORLD-ALLOC-001: new map headers resolve through a resident extension table.

Runs the pinned ARM9 map-header getters on a CPU with the candidate ARM9 and
overlay 129 bytes. GF_AssertFail is a modeled returning leaf; every getter and
the bounds entry are the real bytes.
"""
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'work/tiana-fixes-1/python-tools'))
pytest.importorskip('unicorn')
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_HOOK_CODE
from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_SP, UC_ARM_REG_LR, UC_ARM_REG_PC

from sovereign_editor import character_runtime as cr, scene_runtime as sr, world, world_runtime as wr
from sovereign_editor.formats import EditorError, arm9_code

BASELINE = ROOT/'projects/scyther-orchestration-1/baseline.nds'
STOP = 0x02001000
OUT_X, OUT_Y = 0x022c0000, 0x022c0010


def rom():
    if not BASELINE.exists():
        pytest.skip('Pinned baseline is local and untracked')
    return BASELINE.read_bytes()


def empty():
    return {'files': {}, 'patches': [], 'appends': {}, 'characters': []}


def patched(blob, plan):
    data = bytearray(blob)
    for p in plan['patches']:
        after = bytes.fromhex(p['after'])
        assert data[p['rom_offset']:p['rom_offset']+len(after)] == bytes.fromhex(p['before'])
        data[p['rom_offset']:p['rom_offset']+len(after)] = after
    return bytes(data)


class Machine:
    def __init__(self, arm, extension=None):
        self.u = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
        self.u.mem_map(0x02000000, 0x400000)
        self.u.mem_write(0x02000000, arm)
        if extension is not None:
            self.u.mem_write(0x023d8000, extension)
        self.asserts = 0
        self.u.hook_add(UC_HOOK_CODE, self.leaf)

    def leaf(self, u, address, size, _):
        if address == wr.ASSERT_FAIL:
            self.asserts += 1
            u.reg_write(UC_ARM_REG_PC, u.reg_read(UC_ARM_REG_LR))

    def call(self, entry, header):
        self.u.mem_write(OUT_X, bytes(32))
        for reg, value in ((UC_ARM_REG_R0, header), (UC_ARM_REG_R1, OUT_X), (UC_ARM_REG_R2, OUT_Y),
                           (UC_ARM_REG_SP, 0x02390000), (UC_ARM_REG_LR, STOP | 1)):
            self.u.reg_write(reg, value)
        before = self.asserts
        self.u.emu_start(entry | 1, STOP, count=200)
        assert self.u.reg_read(UC_ARM_REG_PC) == STOP
        return (self.u.reg_read(UC_ARM_REG_R0), bytes(self.u.mem_read(OUT_X, 2)),
                bytes(self.u.mem_read(OUT_Y, 2)), self.asserts - before)


def records(blob):
    arm = arm9_code(blob)
    route = bytearray(arm[world.HEADER_TABLE + 33*24:world.HEADER_TABLE + 34*24])
    room = bytearray(arm[world.HEADER_TABLE + 72*24:world.HEADER_TABLE + 73*24])
    struct.pack_into('<5H', route, 4, 300, 1000, 1001, 1002, 1003)
    route[0] = 150
    struct.pack_into('<H', room, 16, 600)
    return [bytes(route), bytes(room)]


def test_bounds_consumers_are_the_twenty_seven_header_getters():
    arm = arm9_code(rom())
    consumers = wr.consumers(arm)
    assert len(consumers) == 27
    assert consumers[0] == {'call': 0x0203b27e, 'entry': 0x0203b27c}
    assert consumers[-1] == {'call': 0x0203b51e, 'entry': 0x0203b518}


def test_no_created_headers_leave_the_runtime_plan_unchanged():
    blob = rom()
    assert wr.bindings(blob, empty(), []) == empty()


def test_every_getter_reads_created_headers_and_keeps_stock_and_invalid_ids():
    blob = rom(); new = records(blob)
    plan = wr.bindings(blob, empty(), new)
    candidate = patched(blob, plan)
    ext = cr.overlay(blob, 129)
    stock = Machine(arm9_code(blob))
    machine = Machine(arm9_code(candidate), plan['files'][ext['file_id']])
    for consumer in wr.consumers(arm9_code(blob)):
        for header in (0, 1, 33, 72, 539):
            assert machine.call(consumer['entry'], header) == stock.call(consumer['entry'], header)
        for header in (542, 543, 65535):
            result = machine.call(consumer['entry'], header)
            assert result == stock.call(consumer['entry'], header) and result[3] == 1
        for offset, record in enumerate(new):
            # Expected: the stock getter reading the same record from a stock slot.
            reference = bytearray(arm9_code(blob))
            reference[world.HEADER_TABLE + 33*24:world.HEADER_TABLE + 34*24] = record
            expected = Machine(bytes(reference)).call(consumer['entry'], 33)
            assert machine.call(consumer['entry'], 540 + offset) == expected


def test_extension_is_resident_word_aligned_and_reports_its_layout():
    blob = rom(); plan = wr.bindings(blob, empty(), records(blob))
    layout = plan['world_headers']
    ext = cr.overlay(blob, 129)
    assert layout['count'] == 2 and layout['first_id'] == 540
    assert layout['table'] % 8 == 0 and (layout['table'] - wr.TABLE) % 24 == 0
    assert ext['address'] <= layout['hook'] < layout['table'] < ext['address'] + 0x8000
    data = plan['files'][ext['file_id']]
    assert data[:len(ext['data'])] == ext['data']
    start = layout['table'] - ext['address']
    assert data[start:start + 48] == b''.join(records(blob))
    size = [p for p in plan['patches'] if p['rom_offset'] == ext['table_offset'] + 8]
    assert len(size) == 1 and struct.unpack('<I', bytes.fromhex(size[0]['after']))[0] == len(data)


def test_composes_after_the_resident_dispatch_shim():
    blob = rom(); new = records(blob)
    plan = wr.bindings(blob, sr.bindings(blob, empty()), new)
    ext = cr.overlay(blob, 129)
    shim_only = sr.bindings(blob, empty())['files'][ext['file_id']]
    assert plan['files'][ext['file_id']][:len(shim_only)] == shim_only
    assert len([p for p in plan['patches'] if p['rom_offset'] == ext['table_offset'] + 8]) == 1
    machine = Machine(arm9_code(patched(blob, plan)), plan['files'][ext['file_id']])
    stock = Machine(arm9_code(blob))
    reference = bytearray(arm9_code(blob))
    reference[world.HEADER_TABLE + 33*24:world.HEADER_TABLE + 34*24] = new[1]
    matrix = wr.consumers(arm9_code(blob))[2]['entry']
    assert machine.call(matrix, 541) == Machine(bytes(reference)).call(matrix, 33)
    assert machine.call(matrix, 33) == stock.call(matrix, 33)


def test_capacity_and_before_value_refusals_precede_any_plan_change():
    blob = rom(); record = records(blob)[0]
    with pytest.raises(EditorError) as exc:
        wr.bindings(blob, empty(), [record] * (wr.MAX_HEADERS + 1))
    assert exc.value.code == 'RESOURCE_CAPACITY'
    ext = cr.overlay(blob, 129)
    full = empty(); full['files'][ext['file_id']] = ext['data'] + bytes(0x8000 - len(ext['data']) - 16)
    with pytest.raises(EditorError) as exc:
        wr.bindings(blob, full, [record])
    assert exc.value.code == 'RESOURCE_CAPACITY'
    tampered = bytearray(blob)
    start = struct.unpack_from('<I', blob, 0x20)[0]
    tampered[start + wr.BOUNDS - 0x02000000] ^= 1
    with pytest.raises(EditorError) as exc:
        wr.bindings(bytes(tampered), empty(), [record])
    assert exc.value.code in ('BEFORE_VALUE_MISMATCH', 'UNSUPPORTED_RUNTIME')
    with pytest.raises(EditorError) as exc:
        wr.bindings(blob, empty(), [record[:23]])
    assert exc.value.code == 'INVALID_INPUT'


def test_qualification_reference_keeps_the_candidates_own_stock_records():
    """Accepted interior copies rebind stock records (r40 header 72); only the
    extension may differ from the candidate's own table."""
    sys.path.insert(0, str(ROOT/'tools'))
    import world_qualification as wq
    blob = rom(); new = records(blob); plan = wr.bindings(blob, empty(), new)
    arm = bytearray(arm9_code(patched(blob, plan)))
    struct.pack_into('<H', arm, world.HEADER_TABLE + 72 * 24 + 4, 288)   # rebound matrix, as in r40
    extension = plan['files'][cr.overlay(blob, 129)['file_id']]
    report = wq.cpu_parts(bytes(arm), extension, new, arm9_code(blob))
    assert report['failed'] == 0 and report['cases'] == 27 * 10 and report['arm9_bytes_differing_from_reference'] <= 8
    matrix = wr.consumers(arm9_code(blob))[2]['entry']
    assert Machine(bytes(arm), extension).call(matrix, 72)[0] == 288
