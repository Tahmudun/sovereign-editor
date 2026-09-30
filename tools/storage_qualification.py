"""PROD-CAP-001 persistent-storage qualification against the exact pinned build.

Software evidence for the persistent variables and flags the editor allocates
for named states and scene-actor visibility. Not native acceptance.

Consumers examined, all from the pinned baseline ROM unless stated:
  * scripts: every field-script member (a/0/1/2) and every Battle Frontier
    script (a/1/8/2, read by overlay 80), as a u16 at ANY byte offset;
  * events: every coordinate-trigger variable and NPC visibility flag (a/0/3/2);
  * code: ARM9 and all 149 overlays (BLZ-decompressed). Every Thumb call into
    the save var/flag primitives and their pass-through wrappers (BL, literal
    bx/blx tail calls, hg-engine _call_via_rN veneers) is resolved by
    straight-line constant propagation. Unresolved sites were reviewed by hand;
    their bounded computed ranges are listed in COMPUTED with the evidence site;
  * code literals: every 4-aligned u32 word (reported; the STRICT pool excludes
    any match even without a resolved consumer);
  * played saves: every available 512 KiB save, both slots (a value set by play);
  * lifecycle: the exact-build clears — map-temp flags 0..63 and temp vars
    0x4000..0x401F (ARM9 0x02040438), daily flags 0xAA0..0xB5F (0x02040470).

Usage: storage_qualification.py [OUT.json]   (prints a summary)
"""
import hashlib
import json
import struct
import sys
from collections import Counter
from pathlib import Path

import ndspy.codeCompression as cc
import ndspy.narc

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sovereign_editor import character_runtime as cr, world  # noqa: E402
from sovereign_editor.formats import arm9_code, file_span   # noqa: E402

VARS = (0x4000, 0x4170)            # SaveVarsFlags.vars[NUM_VARS = 0x170]
NUM_FLAGS = 0xB60                  # 2912; flags follow vars in the same save block
EVENT_REGION = (0x872, 0x960)      # after stock+authored trainer flags, before system flags
PRIMITIVES = {0x020504A4: ('var', 1),   # Save_VarsFlags_GetVarAddr(state, var)
              0x020503DC: ('flag', 1),  # Save_VarsFlags_CheckFlagInArray
              0x02050408: ('flag', 1),  # Save_VarsFlags_SetFlagInArray
              0x02050430: ('flag', 1),  # Save_VarsFlags_ClearFlagInArray
              0x0205045C: ('flag', 1),  # Save_VarsFlags_GetFlagAddr (range clears)
              0x02040374: ('var', 1),   # GetVarPointer(fsys, var)       (hg-engine rom.ld)
              0x020403AC: ('var', 1),   # VarGet
              0x020403C0: ('var', 1),   # VarSet
              0x02066A7C: ('var', 1),   # SetScriptVarPassSave(state, var, value)
              0x02066AAC: ('var', 1),   # GetScriptVarPassSave
              0x0206659C: ('flag', 1),  # SetScriptFlagPassSave(state, flag)
              0x020665A4: ('flag', 1),  # ClearScriptFlagPassSave
              0x020665AC: ('flag', 1)}  # CheckScriptFlagPassSave
SCRIPT_READ = 0x0203FE2C           # ScriptReadHalfword: IDs read from a script operand
# Computed (base + index / table) accesses found at unresolved call sites.
COMPUTED = [
    ('var', 0x4020, 0x402F, 'object graphics vars 0x4020+index (ARM9 0x020403D8)'),
    ('var', 0x4036, 0x404A, 'sys_vars indexed blocks 0x4036+i, 0x4043+i, 0x4045+i<4 (ARM9 0x02066B9C..0x02066C74)'),
    ('flag', 0x001, 0x03F, 'map-temp flags, cleared on map load (ARM9 0x02040438)'),
    ('flag', 0x11B, 0x11E, '0x11B+i, i<4 (ARM9 0x02066980)'),
    ('flag', 0x320, 0x406, 'hidden-item flags 0x320+id from background events (ARM9 0x0204055C)'),
    ('flag', 0x550, 0x871, 'trainer flags 0x550+trainer for stock and 64 authored IDs (ARM9 0x02040514)'),
    ('flag', 0x960, 0xA9F, 'system flags: computed flypoint/badge/phone/table ranges (0x96B+i, rodata 0x020FE454, ov101 phone table)'),
    ('flag', 0xAA0, 0xB5F, 'daily flags, cleared at day change (ARM9 0x02040470)'),
]
UNTRACED = ['ov2 0x0224F108 reads a flag from 20-byte records loaded through NARC 222; that '
            'archive was not mapped to a path. Mitigated by script absence and save evidence.']


def sha(data):
    return hashlib.sha256(data).hexdigest()


def images(blob):
    out = [('arm9', 0x02000000, arm9_code(blob))]
    start, size = struct.unpack_from('<II', blob, 0x50)
    for off in range(start, start + size, 32):
        e = struct.unpack_from('<8I', blob, off)
        data = bytes(cr.file_by_id(blob, e[6])[1])
        out.append((f'ov{e[0]}', e[1], cc.decompress(data) if e[7] >> 24 & 1 else data))
    return out


# ---- Thumb call-site resolution ------------------------------------------------------

def _hw(data, i):
    return struct.unpack_from('<H', data, i)[0]


def _function_start(data, i, limit=160):
    for back in range(0, limit, 2):
        j = i - back
        if j < 0:
            return None
        h = _hw(data, j)
        if h & 0xFF00 == 0xB500 or h & 0xFF00 == 0xB400 and h & 0xFF:
            return j
    return None


def _branch_targets(data, lo, hi):
    targets, i = set(), lo
    while i < hi:
        h = _hw(data, i)
        if h >> 12 == 0xD and (h >> 8 & 0xF) < 0xE:
            off = h & 0xFF
            targets.add(i + 4 + (off - 256 if off & 0x80 else off) * 2)
        elif h >> 11 == 0x1C:
            off = h & 0x7FF
            targets.add(i + 4 + (off - 2048 if off & 0x400 else off) * 2)
        i += 4 if h >> 11 == 0x1E else 2
    return targets


def _simulate(data, base, lo, call):
    regs = {r: ('arg', r) for r in range(4)}
    regs.update({r: None for r in range(4, 16)})
    defined = {r: lo for r in range(16)}
    i = lo
    while i < call:
        h = _hw(data, i)
        op = h >> 11

        def put(r, v):
            regs[r] = v
            defined[r] = i

        def val(r):
            return regs[r] if isinstance(regs[r], int) else None
        if op == 0x1E:
            h2 = _hw(data, i + 2)
            off = ((h & 0x7FF) << 12) | ((h2 & 0x7FF) << 1)
            off -= (1 << 23) if off & (1 << 22) else 0
            for r in range(4):
                put(r, None)
            put(0, ('ret', base + i + 4 + off))
            i += 4
            continue
        if op == 0x09:
            lit = ((base + i + 4) & ~3) + (h & 0xFF) * 4 - base
            put(h >> 8 & 7, struct.unpack_from('<I', data, lit)[0] if 0 <= lit < len(data) - 3 else None)
        elif op == 0x04:
            put(h >> 8 & 7, h & 0xFF)
        elif op in (0x06, 0x07):
            r = h >> 8 & 7
            put(r, (val(r) + (h & 0xFF) if op == 6 else val(r) - (h & 0xFF)) if val(r) is not None else None)
        elif op in (0x00, 0x01):
            rm, rd, s = h >> 3 & 7, h & 7, h >> 6 & 31
            if val(rm) is not None:
                put(rd, (val(rm) << s) & 0xFFFFFFFF if op == 0 else val(rm) >> (s or 32))
            else:
                put(rd, regs[rm] if op == 0 and s == 0 else None)
        elif op == 0x03:
            rd, rn, x = h & 7, h >> 3 & 7, h >> 6 & 7
            imm, sub = h >> 10 & 1, h >> 9 & 1
            a, b = regs[rn], (x if imm else regs[x])
            if imm and x == 0 and not sub:
                put(rd, a)
            elif isinstance(a, int) and isinstance(b, int):
                put(rd, (a - b if sub else a + b) & 0xFFFFFFFF)
            else:
                put(rd, None)
        elif h & 0xFF00 == 0x4600:
            put((h & 7) | (h >> 4 & 8), regs[h >> 3 & 15])
        elif h & 0xFF80 == 0x4780:
            for r in range(4):
                put(r, None)
        elif op in (0x0D, 0x0F, 0x11, 0x13) or op == 0x0B and h >> 9 & 3 or op == 0x0A and h >> 9 & 1:
            put(h >> 8 & 7 if op == 0x13 else h & 7, None)
        elif op == 0x08 and not h >> 10 & 1 or op == 0x02:
            put(h & 7, None)
        elif op in (0x14, 0x15):
            put(h >> 8 & 7, None)
        i += 2
    return regs, defined


def _calls(data, base, targets):
    veneers = {base + i: _hw(data, i) >> 3 & 15 for i in range(0, len(data) - 1, 2) if _hw(data, i) & 0xFF87 == 0x4700}
    i = 0
    while i < len(data) - 3:
        h = _hw(data, i)
        target = reg = None
        if h >> 11 == 0x1E and _hw(data, i + 2) >> 11 in (0x1F, 0x1D):
            h2 = _hw(data, i + 2)
            off = ((h & 0x7FF) << 12) | ((h2 & 0x7FF) << 1)
            off -= (1 << 23) if off & (1 << 22) else 0
            t = base + i + 4 + off
            if h2 >> 11 == 0x1F and t in targets:
                target = t
            elif h2 >> 11 == 0x1F and t in veneers:
                reg = veneers[t]
            step = 4
        elif h & 0xFF07 in (0x4700, 0x4780) and h & 0x78 != 0x70:
            reg, step = h >> 3 & 15, 2
        else:
            i += 2
            continue
        lo = _function_start(data, i)
        if lo is not None:
            regs, defined = _simulate(data, base, lo, i)
            if reg is not None and isinstance(regs.get(reg), int) and (regs[reg] & ~1) in targets:
                target = regs[reg] & ~1
            if target is not None:
                yield i, target, regs, defined, lo
        i += step


def call_sites(code_images):
    """Resolved constant IDs per kind, script-operand sites and other dynamic sites."""
    targets, found = dict(PRIMITIVES), {}
    for _ in range(6):
        added = {}
        for name, base, data in code_images:
            for site, target, regs, defined, lo in _calls(data, base, targets):
                kind, reg = targets[target]
                v = regs[reg]
                joined = any(defined[reg] < t <= site for t in _branch_targets(data, lo, site))
                key = (name, base + site)
                if base + lo in targets and isinstance(v, tuple) and v[0] == 'arg' and v[1] == targets[base + lo][1]:
                    found[key] = (kind, 'internal')
                elif isinstance(v, int) and not joined:
                    found[key] = (kind, v)
                elif isinstance(v, tuple) and v[0] == 'arg' and base + lo not in targets:
                    added[base + lo] = (kind, v[1])
                    found[key] = (kind, 'wrapper')
                elif isinstance(v, tuple) and v[0] == 'ret' and v[1] == SCRIPT_READ:
                    found[key] = (kind, 'script')
                else:
                    found[key] = (kind, ('dynamic', f'{name}:{base + lo:#x}'))
        added = {k: v for k, v in added.items() if k not in targets}
        if not added:
            break
        targets.update(added)
    return targets, found


# ---- qualification ------------------------------------------------------------------

def played_saves():
    paths = sorted({*ROOT.glob('projects/*/exports/*/game.sav'), ROOT.parent / 'sovereign-gold/test.sav'})
    return [p for p in paths if p.is_file() and p.stat().st_size == 524288]


def save_usage(paths):
    """Nonzero vars / set flags in every valid slot of every played save."""
    used = {}
    for path in paths:
        raw = path.read_bytes()
        for base in (0, 0x40000):
            if struct.unpack_from('<I', raw, base + 65440 - 16 + 8)[0] != 0x20060623:
                continue
            vars_ = base + 4052
            for v in range(*VARS):
                if struct.unpack_from('<H', raw, vars_ + 2 * (v - VARS[0]))[0]:
                    used.setdefault(('var', v), set()).add(path.parent.name)
            flags = vars_ + 2 * (VARS[1] - VARS[0])
            for f in range(1, NUM_FLAGS):
                if raw[flags + f // 8] >> (f % 8) & 1:
                    used.setdefault(('flag', f), set()).add(path.parent.name)
    return used


def qualify(blob, own_vars=range(0x4160, 0x4170)):
    """Evidence rows and strict/relaxed pools for the pinned baseline."""
    assert sha(blob) == cr.BASELINE, 'pinned baseline required'
    code = images(blob)
    literal = {}
    for name, base, data in code:
        for at in range(0, len(data) - 3, 4):
            v = struct.unpack_from('<I', data, at)[0]
            if VARS[0] <= v < VARS[1] or 0 < v < NUM_FLAGS:
                literal.setdefault(v, []).append(f'{name}@{base + at:#x}')
    script_values = {}
    for archive in ('a/0/1/2', 'a/1/8/2'):
        for i, raw in enumerate(ndspy.narc.NARC(bytes(file_span(blob, archive)[1])).files):
            for at in range(len(raw) - 1):
                v = raw[at] | raw[at + 1] << 8
                if VARS[0] <= v < VARS[1] or 0 < v < NUM_FLAGS:
                    script_values.setdefault(v, set()).add(f'{archive}[{i}]')
    triggers, npc_flags = set(), set()
    for raw in ndspy.narc.NARC(bytes(file_span(blob, world.EVENT_ARCHIVE)[1])).files:
        cursor = 0
        for size in (20, 32, 12, 16):
            n = struct.unpack_from('<I', raw, cursor)[0]
            cursor += 4
            for k in range(n):
                at = cursor + k * size
                if size == 32:
                    npc_flags.add(struct.unpack_from('<H', raw, at + 8)[0])
                if size == 16:
                    triggers.add(struct.unpack_from('<H', raw, at + 14)[0])
            cursor += n * size
    targets, sites = call_sites(code)
    # Where is each literal word loaded by code, and does that function reach any
    # var/flag primitive or wrapper? A word never loaded pc-relative is table data.
    by_name = {n: (b, d) for n, b, d in code}
    loads = {}
    for name, base, data in code:
        for i in range(0, len(data) - 1, 2):
            h = _hw(data, i)
            if h >> 11 == 0x09:
                lit = ((base + i + 4) & ~3) + (h & 0xFF) * 4
                loads.setdefault(f'{name}@{lit:#x}', []).append(i)
    callers = {}
    for (name, site), _ in sites.items():
        b, d = by_name[name]
        lo = _function_start(d, site - b)
        if lo is not None:
            callers.setdefault(name, set()).add(lo)

    def literal_use(hit):
        name = hit.split('@')[0]
        b, d = by_name[name]
        users = loads.get(hit, [])
        if not users:
            return 'data'
        starts = {_function_start(d, i) for i in users}
        return 'flag-or-var-function' if starts & callers.get(name, set()) else 'unrelated-function'
    constants = {('var', v): [] for v in ()}
    for (name, site), (kind, v) in sites.items():
        if isinstance(v, int):
            constants.setdefault((kind, v), []).append(f'{name}@{site:#x}')
    dynamic = sorted({v[1] for kind, v in sites.values() if isinstance(v, tuple)})
    saves = played_saves()
    used = save_usage(saves)

    def computed(kind, value):
        return [note for k, lo, hi, note in COMPUTED if k == kind and lo <= value <= hi]

    def row(kind, value):
        return {'scripts': sorted(script_values.get(value, ()))[:4], 'script_count': len(script_values.get(value, ())),
                'events': value in (triggers if kind == 'var' else npc_flags),
                'code_constant': constants.get((kind, value), []), 'computed': computed(kind, value),
                'code_literal': literal.get(value, [])[:4],
                'code_literal_use': sorted({literal_use(h) for h in literal.get(value, [])}),
                'played_saves': sorted(used.get((kind, value), ()))}

    def usable(kind, value, strict):
        r = row(kind, value)
        saves_ok = not r['played_saves'] or kind == 'var' and value in own_vars
        literal_ok = not r['code_literal'] if strict else 'flag-or-var-function' not in r['code_literal_use']
        return (not r['script_count'] and not r['events'] and not r['code_constant'] and not r['computed']
                and saves_ok and literal_ok)

    flags = range(*EVENT_REGION)
    variables = [v for v in range(0x4020, VARS[1]) if v not in own_vars]
    pools = {mode: {'flags': [f for f in flags if usable('flag', f, mode == 'strict')],
                    'vars': [v for v in variables if usable('var', v, mode == 'strict')]}
             for mode in ('strict', 'relaxed')}
    reasons = Counter()
    for f in flags:
        r = row('flag', f)
        reasons['+'.join(k for k in ('script_count', 'events', 'code_constant', 'computed', 'code_literal',
                                      'played_saves') if r[k]) or 'free'] += 1
    return {'baseline_sha256': cr.BASELINE, 'code_images': len(code), 'call_targets': len(targets),
            'call_sites': len(sites), 'script_operand_sites': sum(v == 'script' for _, v in sites.values()),
            'dynamic_functions_reviewed': dynamic, 'computed_ranges': COMPUTED, 'untraced': UNTRACED,
            'resolved_flag_constants': sorted(f'{v:#x}' for k, v in constants if k == 'flag'),
            'resolved_var_constants': sorted(f'{v:#x}' for k, v in constants if k == 'var'),
            'played_saves': [str(p.relative_to(ROOT.parent)) for p in saves],
            'event_region': [f'{EVENT_REGION[0]:#x}', f'{EVENT_REGION[1] - 1:#x}'],
            'event_region_reasons': dict(reasons),
            'pools': {m: {k: [f'{x:#x}' for x in v] for k, v in p.items()} for m, p in pools.items()},
            'rows': {'flag': {f'{f:#x}': row('flag', f) for f in flags},
                     'var': {f'{v:#x}': row('var', v) for v in range(0x4020, VARS[1])}}}


def check_allocation(report, flags, variables):
    """Every allocated flag/var must be in the strict pool of this exact build."""
    strict = report['pools']['strict']
    bad_f = [f'{f:#x}' for f in flags if f'{f:#x}' not in strict['flags']]
    bad_v = [f'{v:#x}' for v in variables if f'{v:#x}' not in strict['vars']]
    return {'flags': len(flags), 'vars': len(variables), 'flags_outside_pool': bad_f, 'vars_outside_pool': bad_v,
            'passed': not bad_f and not bad_v}


# ---- CPU: exact-ARM9 accessors and lifecycle clears ---------------------------------
GET_VAR_ADDR, SET_FLAG, CLEAR_FLAG, CHECK_FLAG = 0x020504A4, 0x02050408, 0x02050430, 0x020503DC
CLEAR_MAPTEMP, CLEAR_DAILY = 0x02040438, 0x02040470      # FieldSystem helpers (ARM9)
SAVE_ARRAY_GET, ASSERT_FAIL, MEMSET = 0x020272C8, 0x0202551C, 0x020E5B44
STATE, FSYS, SAVE, STOP = 0x022D0000, 0x022E0000, 0x022E1000, 0x02001000
FLAGS_AT = 2 * (VARS[1] - VARS[0])                     # flags follow vars[0x170]
BLOCK = FLAGS_AT + NUM_FLAGS // 8                        # sizeof(SaveVarsFlags) = 0x44C


def cpu(blob):
    """Run the pinned ARM9's own var/flag accessors on every allocation, then the
    map-transition and day-change clears, and prove the allocations survive."""
    sys.path.insert(0, str(ROOT / 'work/tiana-fixes-1/python-tools'))
    from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_HOOK_CODE
    from unicorn.arm_const import (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_SP, UC_ARM_REG_LR,
                                   UC_ARM_REG_PC)
    from sovereign_editor import storage
    assert sha(blob) == cr.BASELINE
    u = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
    u.mem_map(0x02000000, 0x400000)
    u.mem_write(0x02000000, arm9_code(blob))
    u.mem_write(FSYS + 0xC, struct.pack('<I', SAVE))
    counts = {'asserts': 0}

    def leaf(uc, address, size, _):
        if address == SAVE_ARRAY_GET:                      # SaveArray_Get(save, SAVE_FLAGS)
            uc.reg_write(UC_ARM_REG_R0, STATE)
        elif address == ASSERT_FAIL:
            counts['asserts'] += 1
        elif address == MEMSET:                            # MI_CpuFill8(dest, value, size)
            dest, value, n = (uc.reg_read(r) for r in (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2))
            uc.mem_write(dest, bytes([value & 0xFF]) * n)
        else:
            return
        uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))
    for address in (SAVE_ARRAY_GET, ASSERT_FAIL, MEMSET):
        u.hook_add(UC_HOOK_CODE, leaf, begin=address, end=address)

    def call(entry, *args):
        for reg, value in zip((UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2), args):
            u.reg_write(reg, value)
        u.reg_write(UC_ARM_REG_SP, 0x02390000)
        u.reg_write(UC_ARM_REG_LR, STOP | 1)
        u.emu_start(entry | 1, STOP, count=2000)
        return u.reg_read(UC_ARM_REG_R0)

    def block():
        return bytes(u.mem_read(STATE, BLOCK))
    rows, failed = [], 0

    def check(name, ok, detail=None):
        nonlocal failed
        rows.append({'check': name, 'pass': bool(ok), **({'detail': detail} if detail is not None else {})})
        failed += not ok
    u.mem_write(STATE, bytes(BLOCK))
    variables = list(storage.STATE_VARS)
    flags = list(storage.HIDE_FLAGS) + list(storage.STATE_FLAGS)
    addresses = {v: call(GET_VAR_ADDR, STATE, v) for v in variables}
    check('every number-state variable resolves inside vars[NUM_VARS]',
          all(a == STATE + 2 * (v - VARS[0]) and a + 2 <= STATE + FLAGS_AT for v, a in addresses.items()))
    for f in flags:
        call(SET_FLAG, STATE, f)
    raw = block()
    check('SetFlag sets exactly the allocated bits inside flags[]',
          {i * 8 + b for i in range(NUM_FLAGS // 8) for b in range(8) if raw[FLAGS_AT + i] >> b & 1} == set(flags)
          and not any(raw[:FLAGS_AT]))
    check('CheckFlag reads every allocated flag as set', all(call(CHECK_FLAG, STATE, f) == 1 for f in flags))
    for f in flags:
        call(CLEAR_FLAG, STATE, f)
    check('ClearFlag clears every allocated flag', not any(block()) and all(call(CHECK_FLAG, STATE, f) == 0 for f in flags))
    # Lifecycle: fill the whole block, run the map-transition and day-change clears.
    u.mem_write(STATE, bytes([0xA5]) * BLOCK)
    call(CLEAR_MAPTEMP, FSYS)
    call(CLEAR_DAILY, FSYS)
    after = block()
    zeroed = {i for i in range(BLOCK) if after[i] == 0}
    expected = set(range(0, 0x40)) | {FLAGS_AT + i for i in range(8)} | {FLAGS_AT + 0xAA0 // 8 + i for i in range(0x18)}
    check('map-transition and day-change clears zero exactly temp vars 0x4000..0x401F, flags 0..63 and 0xAA0..0xB5F',
          zeroed == expected, sorted(zeroed ^ expected)[:8])
    owned = {2 * (v - VARS[0]) + k for v in variables for k in (0, 1)} | {FLAGS_AT + f // 8 for f in flags}
    check('every allocated variable and flag byte survives both clears', not owned & zeroed)
    check('no GF_AssertFail on any access', counts['asserts'] == 0, counts['asserts'])
    check('allocations lie inside sizeof(SaveVarsFlags) = 0x44C, the saved block',
          max(owned) < BLOCK and BLOCK == 0x44C)
    return {'checks': rows, 'passed': sum(r['pass'] for r in rows), 'failed': failed,
            'vars_checked': len(variables), 'flags_checked': len(flags), 'asserts': counts['asserts'],
            'modeled': ['SaveArray_Get returns the test block', 'MI_CpuFill8 (ARM) as memset',
                        'GF_AssertFail returns (retail)'],
            'scope': 'exact pinned ARM9 accessors and clears; save serialization copies the block as is'}


if __name__ == '__main__':
    blob = (ROOT / 'projects/world-authoring-v1/baseline.nds').read_bytes()
    report = qualify(blob)
    from sovereign_editor import storage
    strict_or_reviewed = set(report['pools']['relaxed']['flags'])
    report['allocation'] = {
        'strict': check_allocation(report, storage.STRICT_FLAGS, storage.EXTRA_STATE_VARS),
        'reviewed_flags_in_relaxed_pool': all(f'{f:#x}' in strict_or_reviewed for f in storage.REVIEWED_FLAGS),
        'hide_flags': len(storage.HIDE_FLAGS), 'state_flags': len(storage.STATE_FLAGS), 'state_vars': len(storage.STATE_VARS)}
    report['cpu'] = cpu(blob)
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / 'evidence/production-authoring-v2/storage-qualification.json'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1) + '\n')
    print(json.dumps({k: report[k] for k in ('code_images', 'call_targets', 'call_sites', 'script_operand_sites',
                                             'event_region_reasons', 'allocation')}, indent=1))
    print('cpu', report['cpu']['passed'], 'passed', report['cpu']['failed'], 'failed')
    print({m: {k: len(v) for k, v in p.items()} for m, p in report['pools'].items()})
