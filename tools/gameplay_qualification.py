"""Read-only checks of the delivered Route 30 ROM, not native acceptance.

Executes small ARM/Thumb routines with controlled RTC/RNG leaves under Unicorn.
No emulator UI, graphics, complete battle, or save mutation is involved.
"""
import collections
import json
from pathlib import Path
import struct
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'work/tiana-fixes-1/python-tools')]
import ndspy.rom
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_HOOK_CODE
from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_SP, UC_ARM_REG_LR, UC_ARM_REG_PC, UC_ARM_REG_R4, UC_ARM_REG_R7
from sovereign_editor.formats import arm9_code, resource, digest


def run():
    pair = ROOT / 'projects/gameplay-data-1/exports/route30-r37'
    raw = (pair / 'game.nds').read_bytes()
    accepted = (ROOT / 'projects/scyther-orchestration-1/exports/scyther-r34/game.nds').read_bytes()
    rom = ndspy.rom.NintendoDSRom(raw)
    arm = arm9_code(raw)
    overlays = rom.loadArm9Overlays({2, 129})
    checks = []

    def check(label, value):
        checks.append({'check': label, 'pass': bool(value)})
        assert value, label

    u = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
    u.mem_map(0x02000000, 0x400000)
    u.mem_write(0x02000000, arm)
    for ov in overlays.values():
        u.mem_write(ov.ramAddress, bytes(ov.data))
    controlled = {'rtc': 0, 'rng': 0}
    sentinel = 0x023b0000

    def leaf(cpu, address, size, _):
        cpu.reg_write(UC_ARM_REG_R0, controlled['rtc' if address == 0x0201481c else 'rng'])
        cpu.reg_write(UC_ARM_REG_PC, cpu.reg_read(UC_ARM_REG_LR))

    for address in (0x0201481c, 0x0201fd44):
        u.hook_add(UC_HOOK_CODE, leaf, begin=address, end=address)

    def call(address, r0=0, r1=0):
        u.reg_write(UC_ARM_REG_SP, 0x023b8000)
        u.reg_write(UC_ARM_REG_LR, sentinel | 1)
        u.reg_write(UC_ARM_REG_R0, r0)
        u.reg_write(UC_ARM_REG_R1, r1)
        u.emu_start(address | 1, sentinel, count=20000)
        assert u.reg_read(UC_ARM_REG_PC) == sentinel, 'routine did not return'
        return u.reg_read(UC_ARM_REG_R0)

    # This actual ARM9 function contains the 3000/5000 script-range mapping.
    check('all 737 ordinary trainer scripts map to IDs 1..737',
          all(call(0x020404c8, 3000 + i) == i + 1 for i in range(737)))
    check('Mikey script 3046 maps to trainer 47', call(0x020404c8, 3046) == 47)

    # Decode the actual exported practice-hook branch; Mikey must not gain its flag.
    at = 0x513ac
    a, b = struct.unpack_from('<HH', arm, at)
    check('exported trainer setup contains Thumb BL', a & 0xf800 == 0xf000 and b & 0xf800 == 0xf800)
    delta = ((a & 2047) << 12) | ((b & 2047) << 1)
    if delta & (1 << 22):
        delta -= 1 << 23
    hook = 0x02000000 + at + 4 + delta
    ordinary = []
    for battle_type in (1, 3, 0x13, 0x4b):
        u.reg_write(UC_ARM_REG_R4, battle_type)
        u.reg_write(UC_ARM_REG_R7, 47)
        result = call(hook)
        ordinary.append(result == 11 and u.reg_read(UC_ARM_REG_R4) == battle_type and u.reg_read(UC_ARM_REG_R1) == battle_type)
    check('Mikey bypasses practice-return flag for four battle types', all(ordinary))

    # Execute the ROM's full grass-array initialization for all five RTC codes.
    wild = resource(raw, 'a/0/3/7', 3)[1]
    u.mem_write(0x02390000, wild)
    for time, offset in ((0, 20), (1, 44), (2, 44), (3, 68), (4, 68)):
        controlled['rtc'] = time
        call(0x02246a84, 0x02390000, 0x02391000)
        rows = list(struct.iter_unpack('<IHH', bytes(u.mem_read(0x02391000, 12 * 8))))
        expected = [(struct.unpack_from('<H', wild, offset + 2*i)[0], wild[8+i], wild[8+i]) for i in range(12)]
        check(f'actual grass selector RTC {time}: all species and shared levels', rows == expected)

    weights = {}
    for name, address, expected in (
        ('grass', 0x0224768c, [20, 20, 10, 10, 10, 10, 5, 5, 4, 4, 1, 1]),
        ('surf', 0x02247720, [60, 30, 5, 4, 1]),
        ('fishing', 0x02247764, [40, 30, 15, 10, 5]),
        ('rock_smash', 0x0224779c, [80, 20]),
    ):
        counts = collections.Counter()
        for roll in range(100):
            controlled['rng'] = roll
            counts[call(address)] += 1
        weights[name] = [counts[i] for i in range(len(expected))]
        check(f'actual {name} slot selector: all 100 controlled rolls', weights[name] == expected and sum(counts.values()) == 100)

    # Exact common-script command bytes/branch offsets; no simulated battle outcome.
    script = resource(raw, 'a/0/1/2', 953)[1]
    check('ordinary common script identical to accepted r34', script == resource(accepted, 'a/0/1/2', 953)[1])
    check('Mikey common entry and trainer-number command', struct.unpack_from('<I', script, 46*4)[0] + 46*4+4 == 0xb92
          and script[0xb9a:0xb9e] == struct.pack('<HH', 212, 0x8004))
    check('battle arguments and won-result comparison', script[0xc2f:0xc45] == struct.pack('<HHHHHBBHHHHH', 53, 454, 213, 0x8004, 0, 0, 0, 220, 0x800c, 17, 0x800c, 0))
    check('loss branch targets WhiteOut then releases', script[0xc45:0xc48] == struct.pack('<HB', 28, 1)
          and 0xc4c + struct.unpack_from('<i', script, 0xc48)[0] == 0xd99
          and script[0xd99:0xd9f] == struct.pack('<HHH', 219, 97, 2))
    check('both win paths set the saved trainer defeat flag',
          script[0xc77:0xc7f] == struct.pack('<HHHH', 36, 0x4012, 97, 2)
          and script[0xc7f:0xc83] == struct.pack('<HH', 36, 0x4012))

    report = {'kind': 'bounded exact-ROM CPU and script evidence; native acceptance pending',
              'rom_sha256': digest(raw), 'checks': checks, 'passed': sum(c['pass'] for c in checks), 'failed': sum(not c['pass'] for c in checks),
              'controlled_cases': {'trainer_script_mapping': 737, 'practice_bypass': 4, 'grass_rtc': 5, 'slot_rolls': 400},
              'fixed_slot_weights_percent': weights, 'practice_hook_address': hex(hook),
              'modeled_leaves': ['GF_RTC_GetTimeOfDay returns selected RTC code', 'LCRandom returns selected 0..99 value'],
              'limits': ['No complete battle or loss/blackout execution.', 'No graphics, overworld routing, radio/swarm/ability effects or native emulator execution.', 'Rate modifiers remain engine-owned; weights are not editable record fields.']}
    (ROOT / 'evidence/gameplay-data-1/runtime-qualification.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({k: report[k] for k in ('passed', 'failed', 'controlled_cases', 'fixed_slot_weights_percent')}))


if __name__ == '__main__':
    run()
