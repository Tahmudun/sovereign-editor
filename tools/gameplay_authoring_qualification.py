"""Bounded CPU qualification of the custom species-data consumers. Not native acceptance.

Executes the pinned build's own Thumb code under Unicorn against candidate
archive bytes: the ARM9 personal-field reader, hooked machine mapping and
compatibility, learnset loading/learning (including the four-move replacement
return) and BOTH GetMonEvolutionInternal overlays (133 field, 134 battle).
Modeled leaves: party-Pokémon field access, allocation, archive loads, RTC and
item data. Graphics, message boxes and player input are outside this harness.

Usage: gameplay_authoring_qualification.py PROJECT [OUT.json]
"""
import json
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'work/tiana-fixes-1/python-tools'), str(ROOT / 'work/tiana-events-1/python-tools')]
from unicorn import Uc, UcError, UC_ARCH_ARM, UC_MODE_THUMB, UC_HOOK_CODE
from unicorn.arm_const import (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3, UC_ARM_REG_SP,
                               UC_ARM_REG_LR, UC_ARM_REG_PC)
from sovereign_editor import character_runtime as cr, species as sp
from sovereign_editor.formats import arm9_code, digest

ENGINE = ROOT.parent / 'sovereign-gold/build'
STOP, HEAP, MON, SCRATCH = 0x02001000, 0x022c0000, 0x022b0000, 0x022b8000
REGS = [UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3]
# ARM9 hook entries (hg-engine `ldr rN,[pc]; bx rN; .word target`) and leaves.
HOOKS = {'ItemToMachineMove': 0x02078000, 'ItemToMachineMoveIndex': 0x0207804C,
         'GetMonMachineMoveCompat': 0x0207224C, 'LoadLevelUpLearnset_HandleAlternateForm': 0x02071FC8,
         'MonTryLearnMoveOnLevelUp': 0x02071534}
GET_PERSONAL_ATTR = 0x0206FAA8
LEAVES = {'GetMonData': 0x0206E540, 'SetMonData': 0x0206EC40, 'sys_AllocMemory': 0x0201AA8C,
          'sys_FreeMemoryEz': 0x0201AB0C, 'ArchiveDataLoadOfs': 0x02007560, 'ArchiveDataLoad': 0x02007508,
          'GetItemData': 0x02077D88, 'IsNighttime': 0x02014804, 'GetMoveMaxPP': 0x0207332C,
          # TryAppendMonMove (ARM9, executed) works on the boxed record:
          'Mon_GetBoxMon': 0x02070DB0, 'AcquireBoxMonLock': 0x0206DDD8, 'ReleaseBoxMonLock': 0x0206DE00,
          'GetBoxMonData': 0x0206E640, 'BoxMonSetMoveInSlot': 0x020714F0}
SPECIES, HELD, FRIENDSHIP, BEAUTY, MOVE1, PP1, FORM, LEVEL, ATTACK, DEFENSE = 5, 6, 9, 20, 54, 58, 112, 161, 165, 166
ARC_EVOLUTIONS, ARC_CODE_ADDONS, ARC_LEVELUP = 34, 28, 33


class Cpu:
    def __init__(self, blob, files=None):
        self.u = Uc(UC_ARCH_ARM, UC_MODE_THUMB); self.u.mem_map(0x02000000, 0x400000)
        self.u.mem_write(0x02000000, arm9_code(blob))
        for number in (129,):
            info = cr.overlay(blob, number)
            self.u.mem_write(info['address'], (files or {}).get(info['file_id'], info['data']))
        self.blob = blob; self.heap = HEAP; self.mon = {}; self.archives = {}; self.loads = []; self.calls = []
        self.leaf = {v: k for k, v in LEAVES.items()}
        self.u.hook_add(UC_HOOK_CODE, self.stub)

    def overlay(self, number):
        info = cr.overlay(self.blob, number); self.u.mem_write(info['address'], info['data']); return info

    def r(self, reg): return self.u.reg_read(reg)

    def member(self, arc, index):
        """Candidate bytes when supplied, else the unchanged ROM member (a/0/X/Y = ARC XY)."""
        if (arc, index) in self.archives:
            return self.archives[(arc, index)]
        from sovereign_editor.formats import resource
        return resource(self.blob, f'a/0/{arc // 10}/{arc % 10}', index)[1]

    def stub(self, u, address, size, _):
        name = self.leaf.get(address)
        if name is None:
            return
        a, b, c, d = (self.r(x) for x in REGS); value = 0; self.calls.append(name)
        if name == 'Mon_GetBoxMon':
            value = a
        elif name == 'BoxMonSetMoveInSlot':
            assert a == MON and 0 <= c < 4; self.mon[MOVE1 + c] = b
        elif name in ('GetMonData', 'GetBoxMonData'):
            assert a == MON, 'unexpected Pokémon pointer'
            value = self.mon.get(b, 0)
            if c:
                u.mem_write(c, struct.pack('<I', value))
        elif name == 'SetMonData':
            self.mon[b] = struct.unpack('<I', u.mem_read(c, 4))[0] & 0xffff
        elif name == 'sys_AllocMemory':
            value = self.heap; self.heap += (b + 7) & ~3; u.mem_write(value, b'\xaa' * b)
        elif name == 'ArchiveDataLoadOfs':
            sp_ = struct.unpack('<I', u.mem_read(self.r(UC_ARM_REG_SP), 4))[0]
            data = self.member(b, c); self.loads.append((b, c, d, sp_))
            u.mem_write(a, data[d:d+sp_])
        elif name == 'ArchiveDataLoad':
            data = self.member(b, c); self.loads.append((b, c, 0, len(data))); u.mem_write(a, data)
        elif name == 'GetMoveMaxPP':
            value = 35
        # GetItemData: no hold effect; IsNighttime: daytime; FreeMemory: no-op.
        u.reg_write(UC_ARM_REG_R0, value); u.reg_write(UC_ARM_REG_PC, u.reg_read(UC_ARM_REG_LR))

    def call(self, address, *args, lr=STOP):
        for reg, value in zip(REGS, args):
            self.u.reg_write(reg, value)
        self.u.reg_write(UC_ARM_REG_SP, 0x02390000); self.u.reg_write(UC_ARM_REG_LR, lr | 1)
        self.u.emu_start(address | 1, STOP, count=200000)
        assert self.r(UC_ARM_REG_PC) == STOP, f'routine at {address:#x} did not return'
        return self.r(UC_ARM_REG_R0)


def linked(name):
    from elftools.elf.elffile import ELFFile
    with (ENGINE / 'linked.o').open('rb') as f:
        elf = ELFFile(f); s = elf.get_section_by_name('.symtab').get_symbol_by_name(name)[0]
        sec = elf.get_section(s['st_shndx']); at = (s['st_value'] & ~1) - sec['sh_addr']
        return s['st_value'] & ~1, sec.data()[at:at + s['st_size']]


def run(blob, candidate, species_ids=(161, 162, 16), learn_case=(161, 5, 98)):
    """candidate: {'personal': {s: bytes}, 'evolution': {s: bytes}, 'levelup': bytes, 'machines': bytes}."""
    checks = []

    def check(label, value, detail=None):
        checks.append({'check': label, 'pass': bool(value), **({'detail': detail} if detail is not None else {})})

    arm = arm9_code(blob); ext = cr.overlay(blob, 129)
    # 1. Hook dispatch reaches the linked engine functions, byte-for-byte.
    for name, hook in HOOKS.items():
        word = arm[hook - 0x02000000:hook - 0x02000000 + 8]
        target = struct.unpack_from('<I', word, 4)[0]
        address, code = linked(name)
        at = address - ext['address']
        check(f'{name} hook dispatches to the linked function', word[1] in (0x49, 0x4a, 0x4b) and word[3] == 0x47
              and target == address | 1 and ext['data'][at:at + len(code)] == code, hex(target))
    # 2. ARM9 personal-field reader on candidate records (u16 abilities at 22/26).
    cpu = Cpu(blob)
    for s in species_ids:
        raw = candidate['personal'][s]; cpu.u.mem_write(SCRATCH, raw); view = sp.decode_personal(raw)
        got = [cpu.call(GET_PERSONAL_ATTR, SCRATCH, attr) for attr in (0, 1, 2, 3, 4, 5, 6, 7, 21, 24, 25)]
        want = [*view['stats'].values(), *view['types'], view['growth'], *view['abilities']]
        check(f'species {s} personal fields read by ARM9', got == want, got)
    # 3. Machine item -> index -> move for the qualified base subset.
    moves = [cpu.call(HOOKS['ItemToMachineMove'], 328 + i) for i in sp.BASE_MACHINES]
    indexes = [cpu.call(HOOKS['ItemToMachineMoveIndex'], 328 + i) for i in sp.BASE_MACHINES]
    table = list(struct.unpack_from('<100H', ext['data'], sp.MACHINE_TABLE - ext['address']))
    check('base TM/HM items map to indices 0..99 and the hooked move table',
          indexes == list(sp.BASE_MACHINES) and moves == table and moves[16] == 182, {'TM017': moves[16]})
    # 4. Machine compatibility reads the external row for this species.
    cpu.archives[(ARC_CODE_ADDONS, sp.MACHINE_MEMBER)] = candidate['machines']
    compat = {}
    for s in species_ids:
        cpu.mon = {SPECIES: s, FORM: 0}; cpu.loads.clear()
        compat[s] = cpu.call(HOOKS['GetMonMachineMoveCompat'], MON, 16)
        row = candidate['machines'][s * sp.MACHINE_BYTES:(s + 1) * sp.MACHINE_BYTES]
        check(f'species {s} TM017 compatibility uses its 44-byte row',
              cpu.loads == [(ARC_CODE_ADDONS, sp.MACHINE_MEMBER, s * sp.MACHINE_BYTES, sp.MACHINE_BYTES)]
              and compat[s] == (16 in sp.decode_machines(row)), compat[s])
    # 5. Learnset load and level-up learning, including a full four-move party.
    cpu.archives[(ARC_LEVELUP, 0)] = candidate['levelup']
    buf = HEAP - 0x1000
    for s in species_ids:
        cpu.loads.clear(); cpu.call(HOOKS['LoadLevelUpLearnset_HandleAlternateForm'], s, 0, buf)
        words = struct.unpack('<34I', cpu.u.mem_read(buf, 136))
        rows = sp.decode_learnset(candidate['levelup'][s * 136:(s + 1) * 136])
        check(f'species {s} learnset row loads at species*136',
              [x for x in cpu.loads if x[0] == ARC_LEVELUP] == [(ARC_LEVELUP, 0, s * 136, 136)]
              and [{'level': w >> 16, 'move': w & 0xffff} for w in words[:words.index(0xffff)]] == rows)

    def learn(s, level, known):
        cpu.mon = {SPECIES: s, FORM: 0, LEVEL: level}
        for i, m in enumerate(known + [0] * (4 - len(known))):
            cpu.mon[MOVE1 + i] = m
        cpu.u.mem_write(SCRATCH, b'\0' * 8); results = []
        while True:
            value = cpu.call(HOOKS['MonTryLearnMoveOnLevelUp'], MON, SCRATCH, SCRATCH + 4)
            if not value:
                return results
            results.append((value, struct.unpack('<H', cpu.u.mem_read(SCRATCH + 4, 2))[0]))
    s, level, move = learn_case
    appended = learn(s, level, [10, 111]); slots = [cpu.mon[MOVE1 + i] for i in range(4)]
    full = learn(s, level, [10, 111, 33, 39])
    check(f'species {s} learns move {move} at level {level} into a free slot',
          appended == [(move, move)] and slots == [10, 111, move, 0], {'result': appended, 'slots': slots})
    check('four known moves return the replacement prompt code', full == [(0xffff, move)], full)
    # 6. Both evolution implementations (field overlay 133, battle overlay 134).
    for number in (133, 134):
        info = cpu.overlay(number); outcomes = {}
        for s in species_ids:
            cpu.archives[(ARC_EVOLUTIONS, s)] = candidate['evolution'][s]
            for level in (5, 6, 15, 18):
                cpu.mon = {SPECIES: s, FORM: 0, LEVEL: level, HELD: 0, FRIENDSHIP: 0, BEAUTY: 0, ATTACK: 1, DEFENSE: 1}
                outcomes[f'{s}@{level}'] = cpu.call(info['address'], 0, MON, 0, 0, 0)
        expected = {}
        for s in species_ids:
            rows = [e for e in sp.decode_evolutions(candidate['evolution'][s]) if e['method'] == sp.LEVEL_METHOD]
            for level in (5, 6, 15, 18):
                hits = [e['target'] for e in rows if e['param'] <= level]
                expected[f'{s}@{level}'] = hits[-1] if hits else 0
        check(f'overlay {number} GetMonEvolutionInternal level evolution', outcomes == expected, outcomes)
    return {'scope': 'bounded CPU execution of pinned consumer code; modeled leaves listed in module docstring',
            'checks': checks, 'passed': sum(c['pass'] for c in checks), 'failed': sum(not c['pass'] for c in checks)}


def candidate_from_project(project, species_ids=(161, 162, 16)):
    from sovereign_editor.gameplay import current
    state = project.composed()
    return {'personal': {s: current(project, state, sp.PERSONAL, s) for s in species_ids},
            'evolution': {s: current(project, state, sp.EVOLUTION, s) for s in species_ids},
            'levelup': current(project, state, sp.LEVELUP, 0),
            'machines': current(project, state, sp.ADDONS, sp.MACHINE_MEMBER)}


if __name__ == '__main__':
    from sovereign_editor.core import Project
    project = Project(sys.argv[1])
    report = run(project.blob, candidate_from_project(project))
    report['project_revision'] = project.doc['revision']; report['baseline_sha256'] = digest(project.blob)
    text = json.dumps(report, indent=1)
    if len(sys.argv) > 2:
        Path(sys.argv[2]).write_text(text + '\n')
    print(json.dumps({'passed': report['passed'], 'failed': report['failed']}))
    for c in report['checks']:
        if not c['pass']:
            print('FAIL', c)
