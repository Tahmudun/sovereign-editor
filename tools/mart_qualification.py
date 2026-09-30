"""Exact-ROM special-mart lookup for authored shops (SERVICE-01 qualification). Not native acceptance.

Runs, under Unicorn, the ROM's own code on candidate memory (ARM9 with the runtime-plan
patches applied, and overlay 131 from the plan or an exported ROM):

1. ScrCmd_SpecialMartBuy (ARM9 0x02048158) from its entry to the Mart_Init call at
   0x02048184. Leaves modeled: ScriptReadHalfword (returns var 0x8004) and ScriptGetVar
   (returns the mart index). The list pointer is read from r2 at the call.
2. Mart_Init's list helpers in overlay 3 (decompressed): the counter at 0x02256BEC
   (normal mart: halfwords up to 0xFFFF) and the copier at 0x02256C2C, which drops item 4
   (Poké Ball) while the flag argument (flag 0x9A) is clear.

Usage: mart_qualification.py ROM.nds INDEX [INDEX ...]   (prints JSON)
"""
import json
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'work/tiana-fixes-1/python-tools')]
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_HOOK_CODE  # noqa: E402
from unicorn.arm_const import (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_SP,  # noqa: E402
                               UC_ARM_REG_LR, UC_ARM_REG_PC)
from sovereign_editor import character_runtime as cr  # noqa: E402
from sovereign_editor.formats import arm9_code  # noqa: E402

SPECIAL_MART_BUY, MART_INIT_CALL = 0x02048158, 0x02048184
READ_HALFWORD, GET_VAR = 0x0203FE2C, 0x020403AC
COUNT, COPY = 0x02256BEC, 0x02256C2C
CTX, FIELD, WORK, BUFFER, STACK, STOP = 0x02300000, 0x02300100, 0x02301000, 0x02302000, 0x02390000, 0x02001000


def overlay3(blob):
    import ndspy.codeCompression
    start, size = struct.unpack_from('<II', blob, 0x50)
    for off in range(start, start + size, 32):
        e = struct.unpack_from('<8I', blob, off)
        if e[0] == 3:
            raw = bytes(cr.file_by_id(blob, e[6])[1])
            data = ndspy.codeCompression.decompress(raw) if e[7] >> 24 & 1 else raw
            assert len(data) == e[2]
            return e[1], data
    raise ValueError('overlay 3 absent')


def memory_from_plan(blob, plan):
    """ARM9 and overlay 131 as the exported ROM will load them (patches applied)."""
    arm = bytearray(arm9_code(blob))
    start, base = struct.unpack_from('<I', blob, 0x20)[0], struct.unpack_from('<I', blob, 0x28)[0]
    for p in plan['patches']:
        at = p['rom_offset'] - start
        if 0 <= at < len(arm):
            before, after = bytes.fromhex(p['before']), bytes.fromhex(p['after'])
            assert arm[at:at + len(before)] == before, p['kind']
            arm[at:at + len(after)] = after
    field = cr.overlay(blob, 131)
    return {'arm9': (base, bytes(arm)), 'ov131': (field['address'], plan['files'].get(field['file_id'], field['data'])),
            'ov3': overlay3(blob)}


def memory_from_rom(rom):
    field = cr.overlay(rom, 131)
    return {'arm9': (struct.unpack_from('<I', rom, 0x28)[0], arm9_code(rom)),
            'ov131': (field['address'], field['data']), 'ov3': overlay3(rom)}


class Cpu:
    def __init__(self, memory):
        self.u = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
        self.u.mem_map(0x02000000, 0x400000)
        for address, data in memory.values():
            self.u.mem_write(address, bytes(data))
        self.index = 0
        self.u.hook_add(UC_HOOK_CODE, self.stub)

    def stub(self, u, address, size, _):
        if address in (READ_HALFWORD, GET_VAR):
            u.reg_write(UC_ARM_REG_R0, 0x8004 if address == READ_HALFWORD else self.index)
            u.reg_write(UC_ARM_REG_PC, u.reg_read(UC_ARM_REG_LR))

    def call(self, address, regs, until=STOP):
        for reg, value in zip((UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2), regs):
            self.u.reg_write(reg, value)
        self.u.reg_write(UC_ARM_REG_SP, STACK)
        self.u.reg_write(UC_ARM_REG_LR, STOP | 1)
        self.u.emu_start(address | 1, until, count=200000)
        return self.u.reg_read(UC_ARM_REG_R0)

    def lookup(self, index, pokeball_flag):
        self.index = index
        self.u.mem_write(CTX, b'\0' * 0x100)
        self.u.mem_write(CTX + 0x80, struct.pack('<I', FIELD))
        self.call(SPECIAL_MART_BUY, (CTX, 0, 0), until=MART_INIT_CALL)
        pointer = self.u.reg_read(UC_ARM_REG_R2)
        count = self.call(COUNT, (pointer, 0, 0))
        self.u.mem_write(WORK, b'\0' * 0x300)
        self.u.mem_write(WORK + 0x270, bytes([count]))
        self.u.mem_write(WORK + 0x268, struct.pack('<I', BUFFER))
        self.call(COPY, (WORK, pointer, pokeball_flag))
        kept = self.u.mem_read(WORK + 0x270, 1)[0]
        items = list(struct.unpack(f'<{kept}H', self.u.mem_read(BUFFER, 2 * kept))) if kept else []
        return {'index': index, 'pointer': pointer, 'count': count, 'pokeball_flag': pokeball_flag, 'items': items}


def run(memory, indexes):
    cpu = Cpu(memory)
    return [cpu.lookup(i, flag) for i in indexes for flag in (0, 1)]


if __name__ == '__main__':
    rom = Path(sys.argv[1]).read_bytes()
    print(json.dumps(run(memory_from_rom(rom), [int(a) for a in sys.argv[2:]]), indent=1))
