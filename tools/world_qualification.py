"""WORLD-ALLOC-001 qualification of a candidate ROM. Software evidence, not native acceptance.

CPU: executes every pinned map-header getter (the 27 callers of the bounds check)
from the candidate ARM9 + overlay 129 for stock, created and invalid IDs and
compares with the stock getters reading the same record from a stock slot.
GF_AssertFail is the only modeled leaf (it returns, as in retail builds).

Residency (bytes): Main() loads overlay 129 once through the hg-engine loader;
its RAM stays inside the engine-reserved 0x023D8000..0x023E0000 region, no
other overlay's RAM range intersects it, and the extension table lies inside.

Usage: world_qualification.py CANDIDATE.nds BASELINE.nds [OUT.json]
"""
import json
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'work/tiana-fixes-1/python-tools')]
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_HOOK_CODE
from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_SP, UC_ARM_REG_LR, UC_ARM_REG_PC
from sovereign_editor import character_runtime as cr, world, world_runtime as wr
from sovereign_editor.formats import arm9_code, digest

STOP = 0x02001000
OUT_X, OUT_Y = 0x022c0000, 0x022c0010
MAIN_HOOK, LOADER = 0x02000CD0, 0x02110334
REGION = (0x023D8000, 0x023E0000)


class Machine:
    def __init__(self, arm, extension=None, boot=None):
        self.u = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
        self.u.mem_map(0x02000000, 0x400000)
        self.u.mem_write(0x02000000, arm)
        if extension is not None:
            self.u.mem_write(REGION[0], extension)
        if boot is not None:
            # The resident boot data image (resident.BOOT_REGION), as the boot loader reads it.
            from sovereign_editor import resident
            self.u.mem_write(resident.BOOT_REGION[0], bytes(boot))
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
        assert self.u.reg_read(UC_ARM_REG_PC) == STOP, 'getter did not return'
        return [self.u.reg_read(UC_ARM_REG_R0), bytes(self.u.mem_read(OUT_X, 2)).hex(),
                bytes(self.u.mem_read(OUT_Y, 2)).hex(), self.asserts - before]


def bl(arm, address):
    first, second = struct.unpack_from('<HH', arm, address - 0x02000000)
    return wr._bl_target(address, first, second)


def cpu(candidate, baseline):
    from sovereign_editor import resident
    ext = wr.decode_extension(candidate)
    return cpu_parts(arm9_code(candidate), cr.overlay(candidate, 129)['data'], ext['records'], arm9_code(baseline),
                     resident.boot_image(candidate))


def cpu_parts(arm, extension, records, baseline_arm, boot=None):
    """Candidate getters vs the SAME ARM9 with only the bounds entry restored.

    The reference keeps the candidate's own stock records (accepted interior copies
    rebind some, e.g. r40 header 72), so only the extension's effect is measured.
    """
    at = wr.BOUNDS - 0x02000000
    reference = bytearray(arm)
    reference[at:at + 8] = wr.BOUNDS_BYTES[:8]
    reference = bytes(reference)
    diff = [i for i in range(len(arm)) if arm[i] != reference[i]]
    machine = Machine(arm, extension, boot)
    stock = Machine(reference)
    rows, failures = [], 0
    consumers = wr.consumers(baseline_arm)
    for consumer in consumers:
        entry = consumer['entry']
        for header in (0, 1, 33, 72, 539):
            ok = machine.call(entry, header) == stock.call(entry, header)
            rows.append({'entry': hex(entry), 'header': header, 'kind': 'stock', 'pass': ok}); failures += not ok
        for k, record in enumerate(records):
            slot = bytearray(reference)
            slot[world.HEADER_TABLE + 33 * 24:world.HEADER_TABLE + 34 * 24] = record
            got, expected = machine.call(entry, 540 + k), Machine(bytes(slot)).call(entry, 33)
            ok = got == expected and got[3] == 0
            rows.append({'entry': hex(entry), 'header': 540 + k, 'kind': 'created', 'result': got[:3], 'pass': ok})
            failures += not ok
        for header in (540 + len(records), 540 + wr.MAX_HEADERS, 65535):
            got = machine.call(entry, header)
            ok = got == stock.call(entry, header) and got[3] == 1
            rows.append({'entry': hex(entry), 'header': header, 'kind': 'invalid', 'pass': ok}); failures += not ok
    return {'getters': len(consumers), 'created': len(records), 'cases': len(rows), 'failed': failures, 'rows': rows,
            'reference': 'candidate ARM9 with only the 8-byte bounds entry restored',
            'arm9_bytes_differing_from_reference': len(diff),
            'modeled': ['GF_AssertFail returns (retail behavior); getters otherwise run as real bytes']}


def residency(candidate):
    arm = arm9_code(candidate)
    checks = []

    def add(name, ok, detail=None):
        checks.append({'check': name, 'pass': bool(ok), **({'detail': detail} if detail is not None else {})})
    add('Main() calls the hg-engine ARM9-extension loader', bl(arm, MAIN_HOOK) == LOADER)
    loader = arm[LOADER - 0x02000000:LOADER - 0x02000000 + 8]
    add('loader requests overlay 129 once (HandleLoadOverlay(129, 2))', loader[2:6] == bytes.fromhex('81200221'))
    start, size = struct.unpack_from('<II', candidate, 0x50)
    entries = [struct.unpack_from('<8I', candidate, o) for o in range(start, start + size, 32)]
    own = next(e for e in entries if e[0] == 129)
    add('overlay 129 inside engine reservation', REGION[0] == own[1] and own[1] + own[2] + own[3] <= REGION[1],
        [hex(own[1]), hex(own[1] + own[2] + own[3])])
    clashes = [e[0] for e in entries if e[0] != 129 and e[1] < REGION[1] and e[1] + e[2] + e[3] > REGION[0]]
    add('no other overlay RAM range intersects the reservation', not clashes, clashes)
    ext = wr.decode_extension(candidate)
    if ext['present']:
        add('extension table inside overlay 129', REGION[0] <= ext['table'] and ext['table'] + 24 * ext['count'] <= own[1] + own[2],
            hex(ext['table']))
        add('bounds entry tail-jumps into overlay 129', REGION[0] <= ext['hook'] < REGION[1], hex(ext['hook']))
    return {'checks': checks, 'passed': sum(c['pass'] for c in checks), 'failed': sum(not c['pass'] for c in checks),
            'limits': ['Heap arena bounds and every unload path are not emulated; r40 native acceptance of the '
                       'resident overlay-129 dispatch shim through ordinary loss is the runtime precedent.']}


def main(candidate, baseline, out=None):
    rom, base = Path(candidate).read_bytes(), Path(baseline).read_bytes()
    report = {'candidate_sha256': digest(rom), 'cpu': cpu(rom, base), 'residency': residency(rom)}
    report['failed'] = report['cpu']['failed'] + report['residency']['failed']
    if out:
        Path(out).write_text(json.dumps(report, indent=1) + '\n')
    print(json.dumps({'cases': report['cpu']['cases'], 'residency': report['residency']['passed'], 'failed': report['failed']}))
    return report


if __name__ == '__main__':
    sys.exit(bool(main(*sys.argv[1:])['failed']))
