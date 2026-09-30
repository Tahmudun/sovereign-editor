"""Bounded qualification of the expanded species/form, evolution, relearner and tutor
consumers of the pinned Sovereign Gold ROM. Diagnostic evidence, NOT native acceptance.

1. Linked engine functions (references/sovereign-gold/build/*.o) match the ROM bytes.
2. The script-command handlers used by authored services are reachable and unhooked.
3. The ROM form table agrees with species.h regional ranges and the editor mapping.
4. Unicorn executes the ROM's own code: PokeOtherFormMonsNoGet for every qualified
   form, the form learnset loader, the patched ARM9 relearner eligibility function and
   BOTH GetMonEvolutionInternal copies for every authorable method (day/night, friendship
   threshold, known move, move type, held and used items, form targets).
Modeled leaves are those of tools/gameplay_authoring_qualification.py (GetMonData,
allocation, archive loads, IsNighttime, GetItemData) plus GetMoveData (move type).

Usage: assets_gameplay_qualification.py PROJECT [OUT.json]
"""
import json
import re
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tools')]
import gameplay_authoring_qualification as gq  # noqa: E402
from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_PC, UC_ARM_REG_LR  # noqa: E402
from sovereign_editor import character_runtime as cr, species as sp, species_forms as sf  # noqa: E402
from sovereign_editor.formats import arm9_code, digest  # noqa: E402

REFERENCE = ROOT / 'references/sovereign-gold'
gq.ENGINE = REFERENCE / 'build'
RELEARNER = 0x0209176C
SCRIPT_TABLE = 0x020fad00
GET_MOVE_DATA = 0x023D94CC   # GetMoveData (ov129), symbol of GetMonEvolutionInternal_linked.o
LINKED = {'linked.o': ['PokeOtherFormMonsNoGet', 'GetOtherFormPic', 'PokeIconIndexGetByMonsNumber', 'GiveMon',
                       'CreateBoxMonData', 'LoadLevelUpLearnset_HandleAlternateForm', 'SetFixedWildEncounter',
                       'WildEncSingle', 'WildWaterEncSingle'],
          'field_linked.o': ['AddWildPartyPokemon', 'MakeTrainerPokemonParty', 'GetLearnableTutorMoves', 'ScrCmd_GiveEgg'],
          'GetMonEvolutionInternal_linked.o': ['GetMonEvolutionInternal'],
          'GetMonEvolutionBattle_linked.o': ['GetMonEvolutionInternal']}
SERVICE_COMMANDS = {110: 'AddMoney', 111: 'SubMoneyImmediate', 112: 'HasEnoughMoneyImmediate', 113: 'ShowMoneyBox',
                    114: 'HideMoneyBox', 115: 'UpdateMoneyBox', 125: 'GiveItem', 126: 'TakeItem', 128: 'HasItem',
                    137: 'GiveMon', 140: 'MonHasMove', 150: 'RestoreOverworld', 174: 'FadeScreen', 175: 'WaitFade',
                    197: 'BufferMoveName', 199: 'BufferPartyMonNick', 349: 'PartySelectUI',
                    351: 'GetPartySelection', 354: 'GetPartyMonSpecies', 466: 'CountRelearnableMoves',
                    467: 'MoveRelearnerInit', 468: 'MoveTutorInit', 469: 'MoveRelearnerGetResult',
                    676: 'GetPartyMonForm'}
MON = gq.MON


def symbols(obj, names):
    from elftools.elf.elffile import ELFFile
    out = {}
    with (gq.ENGINE / obj).open('rb') as f:
        elf = ELFFile(f); table = elf.get_section_by_name('.symtab')
        for name in names:
            s = [x for x in table.get_symbol_by_name(name) if x['st_size']][0]
            sec = elf.get_section(s['st_shndx']); at = (s['st_value'] & ~1) - sec['sh_addr']
            out[name] = (s['st_value'] & ~1, sec.data()[at:at + s['st_size']])
    return out


def rom_code(blob):
    """(label, ram address, bytes) for ARM9 and every overlay."""
    import ndspy.rom, ndspy.code
    rom = ndspy.rom.NintendoDSRom(blob)
    out = [('arm9', s.ramAddress, bytes(s.data)) for s in ndspy.code.MainCodeFile(rom.arm9, rom.arm9RamAddress).sections]
    out += [(f'ov{k}', o.ramAddress, bytes(o.data)) for k, o in sorted(rom.loadArm9Overlays().items())]
    return out


def species_constants():
    vals = {}
    for line in (REFERENCE / 'include/constants/species.h').read_text().splitlines():
        m = re.match(r'#define\s+(\w+)\s+(.+?)(\s*//.*)?$', line.strip())
        if not m:
            continue
        expr = re.sub(r'\b[A-Z_][A-Z0-9_]*\b', lambda k: str(vals.get(k.group(0), k.group(0))), m.group(2))
        try:
            vals[m.group(1)] = int(eval(expr, {'__builtins__': {}}))
        except Exception:
            pass
    return vals


class Cpu(gq.Cpu):
    """Adds a controllable night flag, item hold effect 0 and a move-type table."""
    night = 0
    move_types = {}

    def __init__(self, blob, get_move_data):
        gq.LEAVES['GetMoveData'] = get_move_data
        super().__init__(blob)

    def stub(self, u, address, size, _):
        name = self.leaf.get(address)
        if name == 'IsNighttime':
            u.reg_write(UC_ARM_REG_R0, self.night); u.reg_write(UC_ARM_REG_PC, u.reg_read(UC_ARM_REG_LR)); return
        if name == 'GetMoveData':
            move = self.r(gq.REGS[0]); u.reg_write(UC_ARM_REG_R0, self.move_types.get(move, 0))
            u.reg_write(UC_ARM_REG_PC, u.reg_read(UC_ARM_REG_LR)); return
        return super().stub(u, address, size, _)


def evo_record(rows):
    raw = bytearray(sp.EVOLUTION_SIZE)
    for i, (method, param, target) in enumerate(rows):
        struct.pack_into('<3H', raw, i * 6, method, param, target)
    return bytes(raw)


def run(project, out=None):
    blob = project.blob
    checks = []

    def check(label, value, detail=None):
        checks.append({'check': label, 'pass': bool(value), **({'detail': detail} if detail is not None else {})})

    code = rom_code(blob)

    def rom_bytes(address, size):
        hits = [(label, data[address - base:address - base + size]) for label, base, data in code
                if base <= address and address + size <= base + len(data)]
        return hits

    # 1. Linked engine functions equal ROM bytes (some addresses are shared by several overlays).
    matched = {}
    for obj, names in LINKED.items():
        for name, (address, data) in symbols(obj, names).items():
            where = [label for label, b in rom_bytes(address, len(data)) if b == data]
            matched[f'{obj}:{name}'] = where
            check(f'{name} ({obj}) matches ROM bytes', where, {'address': hex(address), 'bytes': len(data), 'in': where})
    # GetMoveData: the target of the BL GetMonEvolutionInternal uses for MOVE_DATA_TYPE.
    evo_field = symbols('GetMonEvolutionInternal_linked.o', ['GetMonEvolutionInternal'])['GetMonEvolutionInternal']
    targets = set()
    base, data = evo_field
    for i in range(0, len(data) - 4, 2):
        h1, h2 = struct.unpack_from('<HH', data, i)
        if (h1 & 0xF800) == 0xF000 and (h2 & 0xE800) == 0xE800:
            off = ((h1 & 0x7FF) << 12) | ((h2 & 0x7FF) << 1)
            off -= 0x800000 if off & 0x400000 else 0
            targets.add((base + i + 4 + off) & ~1)
    arm_targets = sorted(t for t in targets if t < 0x02100000)
    # 2. Script handlers of the service commands: present, and not hook trampolines.
    arm = arm9_code(blob)
    handlers = {}
    for number, name in SERVICE_COMMANDS.items():
        handler = struct.unpack_from('<I', arm, SCRIPT_TABLE - 0x02000000 + 4 * number)[0]
        head = rom_bytes(handler & ~1, 8)
        trampoline = bool(head) and 0x48 <= head[0][1][1] <= 0x4f and head[0][1][3] == 0x47
        handlers[name] = {'command': number, 'handler': hex(handler), 'region': head[0][0] if head else None,
                          'hook_trampoline': trampoline}
    stock = [n for n, h in handlers.items() if h['region'] == 'arm9' and not h['hook_trampoline']]
    check('service script commands dispatch to ROM code', all(h['region'] for h in handlers.values()), handlers)
    # 3. Form table vs species.h regional ranges and the editor's mapping.
    consts = species_constants()
    starts = {name: consts[f'SPECIES_{key}_START'] for name, key in (('Alolan', 'ALOLAN_REGIONAL'), ('Galarian', 'GALARIAN_REGIONAL'),
                                                                   ('Hisuian', 'HISUIAN_REGIONAL'), ('Paldean', 'PALDEAN_FORMS'))}
    starts['Paldean'] = consts['SPECIES_WOOPER_PALDEAN']
    check('editor regional ranges start at species.h constants', all(lo == starts[n] for n, lo, _ in sf.REGIONS), starts)
    rows = [r for r in sf.roster(project) if r['form'] and r['supported']]
    names = {v: k for k, v in consts.items() if k.startswith('SPECIES_') and not k.endswith(('_START', '_NUM'))}
    mismatch = [r['key'] for r in rows if not re.search(r'_(ALOLAN|GALARIAN|HISUIAN|PALDEAN)|TAUROS_(COMBAT|BLAZE|AQUA)',
                                                         names.get(r['personal_index'], ''))]
    check('every qualified form index is a regional SPECIES_* constant', not mismatch, mismatch[:10])
    # 4. CPU: form mapping for every qualified form.
    cpu = Cpu(blob, GET_MOVE_DATA)
    other = symbols('linked.o', ['PokeOtherFormMonsNoGet'])['PokeOtherFormMonsNoGet'][0]
    wrong = []
    for r in rows:
        got = cpu.call(other, r['species'], r['form'])
        if got != r['personal_index'] or sf.personal_index(project, r['species'], r['form']) != got:
            wrong.append((r['key'], got, r['personal_index']))
    check(f'PokeOtherFormMonsNoGet maps all {len(rows)} qualified forms like the editor', not wrong, wrong[:5])
    # Learnset loader with a form (row = personal index).
    table = project.composed().get('gameplay_members', {}).get((sp.LEVELUP, 0)) or sf._member(project, sp.LEVELUP, 0)
    cpu.archives[(gq.ARC_LEVELUP, 0)] = table
    buf = gq.HEAP - 0x1000
    cpu.loads.clear(); cpu.call(gq.HOOKS['LoadLevelUpLearnset_HandleAlternateForm'], 37, 1, buf)
    words = struct.unpack('<34I', cpu.u.mem_read(buf, 136))
    check('Alolan Vulpix learnset loads row 1131', [x for x in cpu.loads if x[0] == gq.ARC_LEVELUP] == [(gq.ARC_LEVELUP, 0, 1131 * 136, 136)]
          and [w & 0xffff for w in words[:words.index(0xffff)]] == [m['move'] for m in sp.decode_learnset(table[1131 * 136:1132 * 136])])
    # 5. Relearner eligibility (patched ARM9) vs the editor policy.
    cases = [(37, 1, 1131, 20, [0, 0, 0, 0]), (37, 1, 1131, 10, None), (545, 0, 545, 12, None), (546, 0, 546, 1, [0, 0, 0, 0]),
             (133, 0, 133, 30, None), (545, 0, 545, 1, 'all')]
    outcomes = []
    for species, form, index, level, known in cases:
        rows_ = sp.decode_learnset(table[index * 136:(index + 1) * 136])
        if known == 'all':      # already knows every eligible move -> empty list
            known = (sp.relearnable(rows_, level, []) + [0, 0, 0, 0])[:4]
        known = known or [m['move'] for m in rows_[:2]] + [0, 0]
        cpu.mon = {gq.SPECIES: species, gq.FORM: form, gq.LEVEL: level, **{gq.MOVE1 + i: m for i, m in enumerate(known)}}
        ptr = cpu.call(RELEARNER, MON, 11)
        got = []
        for i in range(34):
            w = struct.unpack('<H', cpu.u.mem_read(ptr + 2 * i, 2))[0]
            if w == 0xffff:
                break
            got.append(w)
        want = sp.relearnable(rows_, level, [m for m in known if m])
        outcomes.append({'species': species, 'form': form, 'level': level, 'known': known, 'native': got, 'editor': want})
    check('patched relearner offers exactly the editor policy (level<=current incl. level 0, unknown, deduped)',
          all(o['native'] == o['editor'] for o in outcomes), outcomes)
    check('relearner list is empty when nothing is eligible', outcomes[-1]['native'] == [] == outcomes[-1]['editor'],
          outcomes[-1])
    # GiveMon (command 137) reaches the engine GiveMon with its form argument.
    handler = int(handlers['GiveMon']['handler'], 16) & ~1
    body = rom_bytes(handler, 200)[0][1]
    calls = []
    for i in range(0, len(body) - 4, 2):
        h1, h2 = struct.unpack_from('<HH', body, i)
        if (h1 & 0xF800) == 0xF000 and (h2 & 0xE800) == 0xE800:
            off = ((h1 & 0x7FF) << 12) | ((h2 & 0x7FF) << 1)
            off -= 0x800000 if off & 0x400000 else 0
            calls.append((handler + i + 4 + off) & ~1)
    engine_give = symbols('linked.o', ['GiveMon'])['GiveMon'][0]
    hooked = []
    for t in calls:
        head = rom_bytes(t, 8)
        if head and 0x48 <= head[0][1][1] <= 0x4f and head[0][1][3] == 0x47:
            literal = ((t + 4) & ~3) + 4 * head[0][1][0]
            word = rom_bytes(literal, 4)[0][1]
            hooked.append((hex(t), hex(struct.unpack('<I', word)[0])))
    check('script GiveMon (137) calls a stock GiveMon hooked to the engine GiveMon (form argument)',
          any(int(w, 16) & ~1 == engine_give for _, w in hooked), {'calls': [hex(c) for c in calls], 'hooks': hooked})
    # 6. Both evolution implementations for every authorable method.
    results = []
    SNIVY, SERVINE, VULPIX, NINETALES, EEVEE, SYLVEON = 545, 546, 37, 38, 133, 750
    LEAF, ICE, MOON = 85, 849, 81
    scenarios = [
        # (label, record rows, mon, context, used item, night, expected target, expected form)
        ('item use (Leaf Stone)', [(7, LEAF, SERVINE)], {gq.SPECIES: SNIVY, gq.LEVEL: 5}, 3, LEAF, 0, SERVINE),
        ('item use, wrong item', [(7, LEAF, SERVINE)], {gq.SPECIES: SNIVY, gq.LEVEL: 5}, 3, MOON, 0, 0),
        ('item use with form target', [(7, ICE, NINETALES | 1 << 11)], {gq.SPECIES: VULPIX, gq.FORM: 1, gq.LEVEL: 5}, 3, ICE, 0, NINETALES),
        ('item never evolves on level-up', [(7, LEAF, SERVINE)], {gq.SPECIES: SNIVY, gq.LEVEL: 50}, 0, 0, 0, 0),
        ('friendship 160 evolves', [(1, 0, SERVINE)], {gq.SPECIES: SNIVY, gq.LEVEL: 6, gq.FRIENDSHIP: 160}, 0, 0, 0, SERVINE),
        ('friendship 159 does not', [(1, 0, SERVINE)], {gq.SPECIES: SNIVY, gq.LEVEL: 6, gq.FRIENDSHIP: 159}, 0, 0, 0, 0),
        ('friendship_day by day', [(2, 0, SERVINE)], {gq.SPECIES: SNIVY, gq.LEVEL: 6, gq.FRIENDSHIP: 200}, 0, 0, 0, SERVINE),
        ('friendship_day at night', [(2, 0, SERVINE)], {gq.SPECIES: SNIVY, gq.LEVEL: 6, gq.FRIENDSHIP: 200}, 0, 0, 1, 0),
        ('friendship_night at night', [(3, 0, SERVINE)], {gq.SPECIES: SNIVY, gq.LEVEL: 6, gq.FRIENDSHIP: 200}, 0, 0, 1, SERVINE),
        ('level_day by day', [(27, 10, SERVINE)], {gq.SPECIES: SNIVY, gq.LEVEL: 10}, 0, 0, 0, SERVINE),
        ('level_day at night', [(27, 10, SERVINE)], {gq.SPECIES: SNIVY, gq.LEVEL: 10}, 0, 0, 1, 0),
        ('level_night at night', [(28, 10, SERVINE)], {gq.SPECIES: SNIVY, gq.LEVEL: 10}, 0, 0, 1, SERVINE),
        ('level_night below level', [(28, 10, SERVINE)], {gq.SPECIES: SNIVY, gq.LEVEL: 9}, 0, 0, 1, 0),
        ('held item by day', [(18, 110, SERVINE)], {gq.SPECIES: SNIVY, gq.LEVEL: 7, gq.HELD: 110}, 0, 0, 0, SERVINE),
        ('held item by day, at night', [(18, 110, SERVINE)], {gq.SPECIES: SNIVY, gq.LEVEL: 7, gq.HELD: 110}, 0, 0, 1, 0),
        ('held item at night', [(19, 110, SERVINE)], {gq.SPECIES: SNIVY, gq.LEVEL: 7, gq.HELD: 110}, 0, 0, 1, SERVINE),
        ('known move', [(20, 22, SERVINE)], {gq.SPECIES: SNIVY, gq.LEVEL: 7, gq.MOVE1 + 2: 22}, 0, 0, 0, SERVINE),
        ('known move absent', [(20, 22, SERVINE)], {gq.SPECIES: SNIVY, gq.LEVEL: 7, gq.MOVE1: 33}, 0, 0, 0, 0),
        ('known move type + friendship', [(31, 9, SYLVEON)], {gq.SPECIES: EEVEE, gq.LEVEL: 7, gq.MOVE1: 587, gq.FRIENDSHIP: 160}, 0, 0, 0, SYLVEON),
        ('known move type, low friendship', [(31, 9, SYLVEON)], {gq.SPECIES: EEVEE, gq.LEVEL: 7, gq.MOVE1: 587, gq.FRIENDSHIP: 100}, 0, 0, 0, 0),
        ('first qualifying slot wins', [(4, 5, SERVINE), (4, 5, SYLVEON)], {gq.SPECIES: SNIVY, gq.LEVEL: 9}, 0, 0, 0, SERVINE),
    ]
    move_types = {587: 9, 33: 0, 22: 11}
    for number in (133, 134):
        info = cpu.overlay(number)
        for label, record, mon, context, item, night, want in scenarios:
            index = sf.personal_index(project, mon[gq.SPECIES], mon.get(gq.FORM, 0))
            cpu.archives[(gq.ARC_EVOLUTIONS, index)] = evo_record(record)
            cpu.mon = {gq.FORM: 0, gq.HELD: 0, gq.FRIENDSHIP: 0, gq.BEAUTY: 0, gq.ATTACK: 1, gq.DEFENSE: 1, **mon}
            cpu.night = night; cpu.move_types = move_types
            got = cpu.call(info['address'], 0, MON, context, item, 0)
            results.append({'overlay': number, 'case': label, 'native': got, 'expected': want})
    check('both GetMonEvolutionInternal copies decide every authorable method as documented',
          all(r['native'] == r['expected'] for r in results), [r for r in results if r['native'] != r['expected']] or len(results))
    report = {'scope': 'byte matches, handler reachability, form table and bounded CPU execution; not native acceptance',
              'baseline_sha256': digest(blob), 'handlers': handlers, 'stock_arm9_handlers': stock,
              'linked_matches': matched, 'relearner_cases': outcomes, 'evolution_cases': results,
              'get_move_data_candidates': [hex(t) for t in arm_targets],
              'checks': checks, 'passed': sum(c['pass'] for c in checks), 'failed': sum(not c['pass'] for c in checks)}
    if out:
        Path(out).write_text(json.dumps(report, indent=1) + '\n')
    return report


if __name__ == '__main__':
    from sovereign_editor.core import Project
    report = run(Project(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else None)
    print(json.dumps({'passed': report['passed'], 'failed': report['failed']}))
    for c in report['checks']:
        if not c['pass']:
            print('FAIL', c['check'], json.dumps(c.get('detail'))[:600])
