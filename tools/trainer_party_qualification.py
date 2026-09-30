"""Exact-ROM trainer party generation (BATTLE-04 qualification). Not native acceptance.

Runs the pinned build's own MakeTrainerPokemonParty (overlay 131 at 0x023C8038, which is
byte-identical to the engine build's output_field.bin) under Unicorn on candidate trainer
data/party bytes, with the ARM9 and overlay 129 (ARM9 extension) executed natively:
PokeParaSet, Get/SetMonData (encrypted records), personal-data reads, ability, form,
nature, IV/EV and stat recalculation. Modeled leaves: heap allocation (bump), frees
(no-op) and NARC member loads (a/0/X/Y = archive XY, candidate bytes where supplied).
The generated party is read back through the ROM's own GetMonData.

Layout (sovereign-gold include/battle.h, pinned pret BattleSetup): poke_party[n] at
0x04+4n, trainer_id[n] at 0x18+4n, trainer_data[n] (0x34 bytes; data_type, class, -,
count, items[4], aiFlags, doubleBattle) at 0x28+0x34n.

Usage: trainer_party_qualification.py OUT.json (runs the built-in cases)
"""
import json
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'work/tiana-fixes-1/python-tools')]
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_HOOK_CODE  # noqa: E402
from unicorn.arm_const import (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3,  # noqa: E402
                               UC_ARM_REG_SP, UC_ARM_REG_LR, UC_ARM_REG_PC)
from sovereign_editor import character_runtime as cr  # noqa: E402
from sovereign_editor.formats import arm9_code, resource  # noqa: E402

MAKE_PARTY = 0x023C8038
SPECIES_NAME = 0x0200BC28    # engine symbol GetSpeciesNameIntoArray (Thumb 0x0200BC29)
LEAVES = {'ArchiveDataLoad': 0x02007508, 'ArchiveDataLoadMalloc': 0x02007524, 'ArchiveDataLoadOfs': 0x02007560,
          'ArchiveDataLoadMallocOfs': 0x0200757C, 'sys_AllocMemory': 0x0201AA8C, 'sys_AllocMemoryLo': 0x0201AACC,
          'sys_FreeMemoryEz': 0x0201AB0C,
          # Default nickname: opens a message NARC through the file system (not emulated).
          'GetSpeciesNameIntoArray': SPECIES_NAME, 'GetSpeciesName': 0x0200BCDC}
STRING_MAGIC = 0xB6F8D2EC   # pret include/string.h
GET_MON_DATA = 0x0206E540
STOP, HEAP, BP, STACK = 0x02001000, 0x02300000, 0x022F0000, 0x02390000
REGS = [UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3]
PARTY_SIZE, MON_SIZE = 0x600, 0xEC
FIELDS = {'personality': 0, 'species': 5, 'held_item': 6, 'ability': 10, 'form': 112, 'ball': 155, 'level': 161,
          'max_hp': 164, 'attack': 165, 'defense': 166, 'speed': 167, 'sp_attack': 168, 'sp_defense': 169,
          'gender': 111}
IV0, EV0, MOVE0 = 70, 13, 54


class Cpu:
    def __init__(self, blob, members=None):
        self.u = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
        self.u.mem_map(0x02000000, 0x400000)
        self.u.mem_write(0x02000000, arm9_code(blob))
        for number in (129, 131):
            info = cr.overlay(blob, number)
            self.u.mem_write(info['address'], info['data'])
        self.blob, self.members, self.heap, self.loads = blob, dict(members or {}), HEAP, []
        self.leaf = {v: k for k, v in LEAVES.items()}
        self.u.hook_add(UC_HOOK_CODE, self.stub)

    def member(self, arc, index):
        if (arc, index) in self.members:
            return self.members[(arc, index)]
        return resource(self.blob, f'a/0/{arc // 10}/{arc % 10}', index)[1]

    def alloc(self, size):
        at = self.heap; self.heap += (size + 7) & ~3
        self.u.mem_write(at, b'\x00' * size)
        return at

    def stub(self, u, address, size, _):
        name = self.leaf.get(address)
        if name is None:
            return
        a, b, c, d = (u.reg_read(r) for r in REGS)
        sp_ = struct.unpack('<I', u.mem_read(u.reg_read(UC_ARM_REG_SP), 4))[0]
        value = 0
        if name in ('sys_AllocMemory', 'sys_AllocMemoryLo'):
            value = self.alloc(b)
        elif name == 'GetSpeciesNameIntoArray':
            u.mem_write(c, b'\xff\xff')
        elif name == 'GetSpeciesName':
            # An empty, well-formed String (max 11 units, size 0, EOS) for the default nickname.
            value = self.alloc(8 + 2 * 12)
            u.mem_write(value, struct.pack('<HHI', 11, 0, STRING_MAGIC) + b'\xff\xff' * 12)
        elif name == 'ArchiveDataLoad':
            data = self.member(b, c); self.loads.append((b, c)); u.mem_write(a, data)
        elif name == 'ArchiveDataLoadMalloc':
            data = self.member(a, b); self.loads.append((a, b)); value = self.alloc(len(data)); u.mem_write(value, data)
        elif name == 'ArchiveDataLoadOfs':
            data = self.member(b, c); self.loads.append((b, c)); u.mem_write(a, data[d:d + sp_])
        elif name == 'ArchiveDataLoadMallocOfs':
            data = self.member(a, b)[d:d + sp_]; self.loads.append((a, b)); value = self.alloc(len(data))
            u.mem_write(value, data)
        u.reg_write(UC_ARM_REG_R0, value)
        u.reg_write(UC_ARM_REG_PC, u.reg_read(UC_ARM_REG_LR))

    def call(self, address, *args):
        for reg, value in zip(REGS, args):
            self.u.reg_write(reg, value)
        self.u.reg_write(UC_ARM_REG_SP, STACK); self.u.reg_write(UC_ARM_REG_LR, STOP | 1)
        self.u.emu_start(address | 1, STOP, count=20_000_000)
        assert self.u.reg_read(UC_ARM_REG_PC) == STOP, f'routine at {address:#x} did not return'
        return self.u.reg_read(UC_ARM_REG_R0)


def generate(blob, trainer_id, header, party):
    """Run the ROM's party generation for one trainer; return the stored Pokémon values."""
    cpu = Cpu(blob, {(56, trainer_id): party, (55, trainer_id): header})
    bp = BP
    cpu.u.mem_write(bp, b'\x00' * 0x200)
    parties = [cpu.alloc(PARTY_SIZE) for _ in range(4)]
    for n, at in enumerate(parties):
        cpu.u.mem_write(bp + 0x04 + 4 * n, struct.pack('<I', at))
        cpu.u.mem_write(at, struct.pack('<ii', 6, 0))
    cpu.u.mem_write(bp + 0x18 + 4, struct.pack('<i', trainer_id))
    cpu.u.mem_write(bp + 0x28 + 0x34, header[:20])
    cpu.call(MAKE_PARTY, bp, 1, 11)
    enemy = parties[1]
    count = struct.unpack('<i', cpu.u.mem_read(enemy + 4, 4))[0]
    mons = []
    for i in range(count):
        mon = enemy + 8 + MON_SIZE * i
        row = {k: cpu.call(GET_MON_DATA, mon, v, 0) for k, v in FIELDS.items()}
        row['ivs'] = [cpu.call(GET_MON_DATA, mon, IV0 + j, 0) for j in range(6)]
        row['evs'] = [cpu.call(GET_MON_DATA, mon, EV0 + j, 0) for j in range(6)]
        row['moves'] = [cpu.call(GET_MON_DATA, mon, MOVE0 + j, 0) for j in range(4)]
        row['nature'] = row['personality'] % 25
        mons.append(row)
    return {'count': count, 'mons': mons, 'loads': cpu.loads}


NATURE_UP_DOWN = {n: (n // 5, n % 5) for n in range(25)}   # (raised, lowered) among Atk, Def, Spe, SpA, SpD


def expected_stats(base, level, ivs, evs, nature):
    """Gen 4 stat formula (independent of the ROM) for comparison with the generated party."""
    hp = (2 * base[0] + ivs[0] + evs[0] // 4) * level // 100 + level + 10
    up, down = NATURE_UP_DOWN[nature]
    out = [hp]
    for i in range(1, 6):
        value = (2 * base[i] + ivs[i] + evs[i] // 4) * level // 100 + 5
        k = i - 1          # 0 Atk, 1 Def, 2 Spe, 3 SpA, 4 SpD (personal/IV order)
        if up != down and k == up:
            value = value * 110 // 100
        elif up != down and k == down:
            value = value * 90 // 100
        out.append(value)
    return out


def cases():
    common = {'moves': [33, 0, 0, 0], 'held_item': 0}
    return [
        {'name': 'default fields', 'trainer_id': 750, 'class': 2,
         'trainer': {'party': [{'species': 161, 'level': 12, **common}, {'species': 16, 'level': 10, **common}]}},
        {'name': 'nature, IV/EV, hidden ability, ball', 'trainer_id': 751, 'class': 2,
         'trainer': {'ai': ['prioritize_super_effective', 'evaluate_attacks'], 'items': [17],
                     'party': [{'species': 161, 'level': 30, **common, 'nature': 3, 'ability': 'hidden', 'ball': 2,
                                'ivs': [31, 30, 29, 28, 27, 26], 'evs': [252, 0, 4, 252, 0, 0]},
                               {'species': 16, 'level': 25, **common, 'nature': 15, 'ability': 'second', 'ball': 4,
                                'ivs': [0, 0, 0, 0, 0, 0], 'evs': [0, 0, 0, 0, 0, 0]}]}},
        {'name': 'regional form hidden ability and boundaries', 'trainer_id': 752, 'class': 2,
         'trainer': {'battle': 'double',
                     'party': [{'species': 37, 'form': 1, 'level': 100, **common, 'nature': 24, 'ability': 'hidden',
                                'ball': 1, 'ivs': [31] * 6, 'evs': [252, 252, 6, 0, 0, 0]},
                               {'species': 263, 'form': 1, 'level': 1, **common, 'nature': 0, 'ability': 'first',
                                'ball': 3, 'ivs': [0] * 6, 'evs': [0] * 6}]}},
        # DATA-03: a qualified expanded move and expanded held item reach the generated party.
        {'name': 'expanded move and held item', 'trainer_id': 753, 'class': 2,
         'trainer': {'party': [{'species': 161, 'level': 20, 'moves': [471, 33, 0, 0], 'held_item': 538}]}},
        # The build's BLOCK_LEARNING_UNIMPLEMENTED_MOVES: an unimplemented move (FLAG_UNUSED_MOVE,
        # which the editor refuses) is zeroed in the generated party. Encoded without validation.
        {'name': 'engine zeroes an unimplemented move', 'trainer_id': 754, 'class': 2, 'validate': False,
         'expect_moves': [0, 33, 0, 0],
         'trainer': {'party': [{'species': 161, 'level': 20, 'moves': [475, 33, 0, 0], 'held_item': 0}]}},
    ]


def main(out):
    from sovereign_editor.core import Project
    from sovereign_editor import trainer_format as tf, species as sp, species_forms as sf
    project = Project(ROOT / 'projects/assets-gameplay-v1')
    blob = project.blob
    results = []
    for case in cases():
        if case.get('validate', True):
            tf.validate(project, case['trainer'])
        header, party = tf.encode(case['trainer'], case['class'])
        got = generate(blob, case['trainer_id'], header, party)
        checks = [('count', got['count'] == len(case['trainer']['party']))]
        checks.append(('double word', header[16] == (2 if case['trainer'].get('battle') == 'double' else 0)))
        for want, mon in zip(case['trainer']['party'], got['mons']):
            first, second, hidden = tf.abilities(project, want['species'], want.get('form', 0))
            slot = want.get('ability', 'first')
            ability = {'first': first, 'second': second, 'hidden': hidden}[slot]
            tag = f"{want['species']}:{want.get('form', 0)}"
            checks += [(f'{tag} species/form/level', (mon['species'], mon['form'], mon['level'])
                        == (want['species'], want.get('form', 0), want['level'])),
                       (f'{tag} ability {slot}', mon['ability'] == ability)]
            if 'ivs' in want:
                checks += [(f'{tag} IVs', mon['ivs'] == want['ivs']), (f'{tag} EVs', mon['evs'] == want['evs'])]
            else:
                checks.append((f'{tag} default IVs 0', mon['ivs'] == [0] * 6))
            if 'nature' in want:
                checks.append((f'{tag} nature', mon['nature'] == want['nature']))
            if want.get('moves'):
                checks.append((f'{tag} moves', mon['moves'] == case.get('expect_moves', want['moves'])))
            if want.get('held_item'):
                checks.append((f'{tag} held item', mon['held_item'] == want['held_item']))
            if 'ball' in want:
                checks.append((f'{tag} ball', mon['ball'] == want['ball']))
            index = sf.personal_index(project, want['species'], want.get('form', 0))
            base = list(sp.decode_personal(resource(blob, 'a/0/0/2', index)[1])['stats'].values())
            stats = [mon['max_hp'], mon['attack'], mon['defense'], mon['speed'], mon['sp_attack'], mon['sp_defense']]
            want_stats = expected_stats(base, want['level'], want.get('ivs', [0] * 6), want.get('evs', [0] * 6),
                                        mon['nature'])
            checks.append((f'{tag} stats recalculated', stats == want_stats))
            mon['expected_stats'] = want_stats
        results.append({'case': case['name'], 'header': header.hex(), 'party': party.hex(),
                        'generated': got['mons'], 'checks': [{'check': c, 'pass': ok} for c, ok in checks],
                        'pass': all(ok for _, ok in checks)})
    report = {'harness': __doc__.splitlines()[0], 'pinned_baseline': project.doc['baseline']['sha256'],
              'overlay131_matches_engine_build': cr.overlay(blob, 131)['data'] == (
                  ROOT.parent / 'sovereign-gold/build/output_field.bin').read_bytes(),
              'results': results,
              'passed': sum(r['pass'] for r in results), 'failed': sum(not r['pass'] for r in results)}
    Path(out).write_text(json.dumps(report, indent=1))
    print(json.dumps({r['case']: [c['check'] for c in r['checks'] if not c['pass']] or 'pass' for r in results}))


if __name__ == '__main__':
    main(sys.argv[1])
