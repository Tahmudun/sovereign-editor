"""Independent readback of terrain authoring v1 from exported ROMs. Evidence, not native acceptance.

Compares a candidate ROM against a reference (the accepted r56 export):

* map archive a/0/6/5: every member except the changed ones must be byte-identical;
* changed members: section table, sound-plate count, buildings unchanged, permission pairs
  equal to the expected feature roles and unchanged elsewhere;
* heights: the real overlay-1 Thumb routine ov01_021FAE50 (BDHC lookup) runs in Unicorn on
  every tile centre of every area cell, candidate and reference; tiles outside the features
  must match the reference, feature tiles must equal the expected terrace/stair/water heights
  (FX_Div, the hardware-divider wrapper, is hooked with the same (a << 12) / b formula);
* movement: whole-area flood from the arrival warps with the runtime step rule
  (sub_02054954: |dh| >= 1.25 tile blocks, else the permission collision bit), on foot and
  with Surf, using those CPU heights and the exported permissions. Every tile reachable in
  the reference outside the features stays reachable; terrace tops are reachable only
  through their stairs; rims are never entered; Surf ponds are reachable only by Surf.

Usage: terrain_readback.py CANDIDATE.nds REFERENCE.nds SPEC.json [OUT.json]
"""
import json
import struct
import sys
from pathlib import Path

import ndspy.rom
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_HOOK_CODE
from unicorn.arm_const import (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3, UC_ARM_REG_SP,
                               UC_ARM_REG_LR, UC_ARM_REG_PC)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sovereign_editor.formats import arm9_code, digest, member_count, resource  # noqa: E402

MAP_ARCHIVE = 'a/0/6/5'
LOOKUP = 0x021FAE50
STOP = 0x02001000
MEM = 0x02380000
FX = 65536
SURFABLE = {0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x19, 0x2A, 0x50, 0x51, 0x52, 0x53, 0x73, 0x78, 0x7C}


def sections(raw):
    perm, bld, mdl, ter = struct.unpack_from('<4I', raw)
    sig, extra = struct.unpack_from('<HH', raw, 16)
    assert sig == 0x1234 and perm == 2048 and 20 + extra + perm + bld + mdl + ter == len(raw)
    s = 20 + extra
    return {'bgs': raw[20:s], 'perm': raw[s:s + perm], 'bld': raw[s + perm:s + perm + bld],
            'model': raw[s + perm + bld:s + perm + bld + mdl], 'bdhc': raw[s + perm + bld + mdl:]}


class Heights:
    """The pinned overlay-1 BDHC lookup, executed on the ROM's own Thumb bytes."""

    def __init__(self, blob):
        rom = ndspy.rom.NintendoDSRom(blob)
        ov1 = rom.loadArm9Overlays([1])[1]
        code, base = bytes(ov1.data), ov1.ramAddress
        self.u = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
        self.u.mem_map(0x02000000, 0x400000)
        self.u.mem_write(0x02000000, bytes(arm9_code(blob)))
        self.u.mem_write(base, code)
        at = code.find(b'\x40\x42', LOOKUP - base, LOOKUP - base + 0x200)     # neg r0, r0
        hi, lo = struct.unpack_from('<HH', code, at + 2)
        assert hi >> 11 == 0x1E and lo >> 11 in (0x1F, 0x1D), 'FX_Div call not found'
        off = ((hi & 0x7FF) << 12) | ((lo & 0x7FF) << 1)
        off -= 0x800000 if off & 0x400000 else 0
        target = base + at + 2 + 4 + off
        # BLX (second half 0b11101) switches to ARM: the target is word aligned.
        self.fx_div = target & ~3 if lo >> 11 == 0x1D else target & ~1
        self.u.hook_add(UC_HOOK_CODE, self._hook)
        self.calls = 0

    def _hook(self, u, address, size, _):
        if address == self.fx_div:
            signed = lambda v: v - (1 << 32) if v & 0x80000000 else v
            a, b = signed(u.reg_read(UC_ARM_REG_R0)), signed(u.reg_read(UC_ARM_REG_R1))
            q = abs(a << 12) // abs(b) * (1 if (a < 0) == (b < 0) else -1) if b else 0
            u.reg_write(UC_ARM_REG_R0, q & 0xFFFFFFFF)
            u.reg_write(UC_ARM_REG_PC, u.reg_read(UC_ARM_REG_LR))

    def load(self, bdhc):
        counts = struct.unpack_from('<6H', bdhc, 4)
        cursor, ptrs, addr = 16, [], MEM + 0x100
        for n, s in zip(counts, (8, 12, 4, 8, 8, 2)):
            chunk = bdhc[cursor:cursor + n * s]
            self.u.mem_write(addr, chunk + b'\0' * 4)
            ptrs.append(addr)
            addr += (len(chunk) + 16) & ~3
            cursor += n * s
        points, normals, constants, plates, strips, indices = ptrs
        self.u.mem_write(MEM, struct.pack('<8I', plates, constants, strips, indices, points, normals, 1, counts[4]))

    def at(self, x, z, current=0, mode=0):
        u, sp = self.u, 0x02390000
        u.mem_write(sp, struct.pack('<2I', MEM, MEM + 0x80))
        for reg, value in zip((UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3), (mode, current, x, z)):
            u.reg_write(reg, value & 0xFFFFFFFF)
        u.reg_write(UC_ARM_REG_SP, sp)
        u.reg_write(UC_ARM_REG_LR, STOP | 1)
        u.emu_start(LOOKUP | 1, STOP, count=20000)
        self.calls += 1
        if not u.reg_read(UC_ARM_REG_R0):
            return None
        return struct.unpack('<i', u.mem_read(MEM + 0x80, 4))[0] / FX

    def grid(self, bdhc):
        self.load(bdhc)
        return {(tx, tz): self.at(int((tx - 15.5) * FX), int((tz - 15.5) * FX)) for tz in range(32) for tx in range(32)}


def _polys(model):
    from sovereign_editor import nitro
    _, prims = nitro.decode_model(model, geometry_only=True, render=False)
    out = []
    for p in prims:
        for poly in p.polygons:
            v = p.vertices[poly]
            out.append((p.material.get('name'), tuple(tuple(round(float(a), 3) for a in row) for row in v)))
    return out


def polygons_outside(reference, candidate, origin, boxes):
    """Reference polygons whose XZ bounds avoid every feature box must survive exactly."""
    ox, oz = origin
    def outside(poly):
        xs = [(v[0] + 256) / 16 + ox for v in poly[1]]
        zs = [(v[2] + 256) / 16 + oz for v in poly[1]]
        return all(max(xs) <= b[0] + 1e-6 or min(xs) >= b[2] - 1e-6 or max(zs) <= b[1] + 1e-6 or min(zs) >= b[3] - 1e-6
                   for b in boxes)
    from collections import Counter
    ref = Counter(p for p in _polys(reference) if outside(p))
    cand_all = _polys(candidate)
    cand = Counter(p for p in cand_all if outside(p))
    missing = [list(p[1][0]) for p, n in (ref - cand).items()]
    return sum(ref.values()), missing, len(cand_all) - sum(cand.values())


def area_tiles(blob, spec, cpu):
    pairs, heights, raws = {}, {}, {}
    for key, (ox, oz) in spec['cells'].items():
        raw = resource(blob, MAP_ARCHIVE, int(key))[1]
        sec = sections(raw)
        raws[int(key)] = sec
        h = cpu.grid(sec['bdhc'])
        for tz in range(32):
            for tx in range(32):
                i = (tz * 32 + tx) * 2
                pairs[(ox + tx, oz + tz)] = sec['perm'][i:i + 2]
                heights[(ox + tx, oz + tz)] = h[(tx, tz)]
    return pairs, heights, raws


def flood(pairs, heights, starts, surf):
    ok = lambda t: t in pairs and not pairs[t][1] & 0x80 and heights[t] is not None and (
        surf or pairs[t][0] not in SURFABLE)
    seen = {s for s in starts if ok(s)}
    todo = list(seen)
    while todo:
        x, z = todo.pop()
        for n in ((x + 1, z), (x - 1, z), (x, z + 1), (x, z - 1)):
            if n not in seen and ok(n) and abs(heights[n] - heights[(x, z)]) < 1.25:
                seen.add(n)
                todo.append(n)
    return seen


def main(candidate, reference, spec_path, out_path=None):
    cand, ref = Path(candidate).read_bytes(), Path(reference).read_bytes()
    spec = json.loads(Path(spec_path).read_text())
    report = {'candidate_sha256': digest(cand), 'reference_sha256': digest(ref), 'checks': [], 'failures': []}

    def check(name, ok, **detail):
        report['checks'].append({'name': name, 'pass': bool(ok), **detail})
        if not ok:
            report['failures'].append(name)

    count = member_count(cand, MAP_ARCHIVE)
    changed = {int(k) for k in spec['changed']}
    check('map member count unchanged', count == member_count(ref, MAP_ARCHIVE), count=count)
    same = sum(resource(cand, MAP_ARCHIVE, i)[1] == resource(ref, MAP_ARCHIVE, i)[1]
               for i in range(count) if i not in changed)
    check('every other map member byte-identical', same == count - len(changed), identical=same)
    cpu_c, cpu_r = Heights(cand), Heights(ref)
    pc, hc, rc = area_tiles(cand, spec, cpu_c)
    pr, hr, rr = area_tiles(ref, spec, cpu_r)
    roles = {tuple(map(int, k.split(','))): v for k, v in spec['roles'].items()}
    for m in sorted(changed):
        c, r = rc[m], rr[m]
        check(f'member {m} sound plates', len(c['bgs']) // 8 == spec['changed'][str(m)]['plates'],
              plates=len(c['bgs']) // 8, reference=len(r['bgs']) // 8)
        check(f'member {m} buildings unchanged', c['bld'] == r['bld'])
        check(f'member {m} model changed only where authored', (c['model'] != r['model']) == spec['changed'][str(m)]['model'])
        if c['model'] != r['model']:
            kept, lost, new = polygons_outside(r['model'], c['model'], spec['cells'][str(m)], spec['clear'])
            check(f'member {m} polygons outside feature boxes unchanged', not lost,
                  reference_outside=kept, missing=lost[:4], added_inside=new)
    bad_pair = [[*t, pc[t].hex(), v['pair']] for t, v in roles.items() if pc[t].hex() != v['pair']]
    other_pair = [[*t, pr[t].hex(), pc[t].hex()] for t in pc if t not in roles and pc[t] != pr[t]]
    check('feature permission pairs', not bad_pair, tiles=len(roles), mismatches=bad_pair[:8])
    check('permissions outside features unchanged', not other_pair, mismatches=other_pair[:8])
    bad_h = [[*t, hc[t], v['height']] for t, v in roles.items()
             if hc[t] is None or abs(hc[t] - v['height']) > 1 / 256]
    moved = [[*t, hr[t], hc[t]] for t in hc if t not in roles and hc[t] != hr[t]]
    check('CPU heights on feature tiles', not bad_h, samples=len(roles), mismatches=bad_h[:8])
    check('CPU heights elsewhere unchanged', not moved, samples=len(hc) - len(roles), mismatches=moved[:8])
    starts = sorted({n for x, z in spec['starts'] for n in ((x, z), (x + 1, z), (x - 1, z), (x, z + 1), (x, z - 1))})
    foot_r, foot_c = flood(pr, hr, starts, False), flood(pc, hc, starts, False)
    surf_c = flood(pc, hc, starts, True)
    lost = sorted(t for t in foot_r - foot_c if t not in roles)
    check('no previously reachable tile stranded', not lost, lost=[list(t) for t in lost[:8]], reachable=len(foot_c))
    for name, want in (('top', 'foot'), ('stair', 'foot'), ('rim', 'never'), ('water', 'surf'),
                       ('decorative_water', 'never')):
        tiles = [t for t, v in roles.items() if v['role'] == name]
        if not tiles:
            continue
        if want == 'foot':
            ok = all(t in foot_c for t in tiles)
        elif want == 'never':
            ok = not any(t in surf_c for t in tiles)
        else:
            ok = all(t in surf_c and t not in foot_c for t in tiles)
        check(f'{name} tiles reachable: {want}', ok, tiles=len(tiles))
    # Terrace tops only through stairs: removing stair tiles must cut the top off.
    stairs = {t for t, v in roles.items() if v['role'] == 'stair'}
    tops = {t for t, v in roles.items() if v['role'] == 'top'}
    if tops:
        blocked = {t: (bytes((p[0], p[1] | 0x80)) if t in stairs else p) for t, p in pc.items()}
        check('terrace top reachable only through stairs', not (flood(blocked, hc, starts, False) & tops))
    seam = spec.get('seam_edges', [])
    steps = []
    for a, b in seam:
        a, b = tuple(a), tuple(b)
        forward = not pc[b][1] & 0x80 and abs(hc[b] - hc[a]) < 1.25
        back = not pc[a][1] & 0x80 and abs(hc[a] - hc[b]) < 1.25
        steps.append([list(a), list(b), hc[a], hc[b], forward, back])
    check('cross-seam steps passable both ways', all(s[4] and s[5] for s in steps), steps=steps)
    report['cpu_calls'] = cpu_c.calls + cpu_r.calls
    report['fx_div'] = hex(cpu_c.fx_div)
    if out_path:
        Path(out_path).write_text(json.dumps(report, indent=1))
    print(json.dumps({'checks': len(report['checks']), 'passed': len(report['checks']) - len(report['failures']),
                      'failures': report['failures'], 'cpu_calls': report['cpu_calls']}, indent=1))
    return report


if __name__ == '__main__':
    main(*sys.argv[1:])
