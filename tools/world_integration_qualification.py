"""World integration v1 CPU qualification. Software evidence, not native acceptance.

Runs the real Thumb bytes in Unicorn, candidate against reference, for:

* ARM9 0x02054E20 (is a map member excluded from the area texture animation): every member
  ID 0..max+1. Reference = stock bytes; candidate must agree except exactly on the created
  static members, which must now be excluded.
* overlay 101 PokegearMap_GetLocationSpecByCoord (0x021EA6E8) over the whole Pokégear grid
  (x 0..47, y 0..20) with a zeroed app struct whose spec pointer/count are the stock table:
  stock hits must be identical, misses must return the created spec covering the cell, else NULL.
* map-header getters (all 27 bounds-check consumers) through tools/world_qualification.cpu_parts,
  which also exercises layout v3 identity overrides for every created header.

Images come from a candidate ROM or directly from a runtime plan (before export).
Usage: world_integration_qualification.py CANDIDATE.nds BASELINE.nds [OUT.json]
"""
import json
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tools'), str(ROOT / 'work/tiana-fixes-1/python-tools')]
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_HOOK_CODE
from unicorn.arm_const import (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_SP, UC_ARM_REG_LR,
                               UC_ARM_REG_PC)
from sovereign_editor import character_runtime as cr, world_runtime as wr
from sovereign_editor.formats import arm9_code, digest

STOP = 0x02001000
APP = 0x02300000             # zeroed PokegearMapAppData stand-in
SPECS_FIELD, COUNT_FIELD = 0x214, 0x136
TOWN_ENTRY = 0x021EA6E8
GRID = (48, 21)


class Machine:
    def __init__(self, arm, ov101=None, ov129=None, boot=None):
        self.u = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
        self.u.mem_map(0x02000000, 0x400000)
        self.u.mem_write(0x02000000, bytes(arm))
        if ov101 is not None:
            self.u.mem_write(wr.TOWN_ADDRESS, bytes(ov101))
        if ov129 is not None:
            self.u.mem_write(0x023D8000, bytes(ov129))
        if boot is not None:
            from sovereign_editor import resident
            self.u.mem_write(resident.BOOT_REGION[0], bytes(boot))   # the resident boot data image
        self.asserts = 0
        self.u.hook_add(UC_HOOK_CODE, self.leaf)

    def leaf(self, u, address, size, _):
        if address == wr.ASSERT_FAIL:
            self.asserts += 1
            u.reg_write(UC_ARM_REG_PC, u.reg_read(UC_ARM_REG_LR))

    def call(self, entry, *args, limit=4000):
        regs = (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2)
        for reg, value in zip(regs, args):
            self.u.reg_write(reg, value)
        self.u.reg_write(UC_ARM_REG_SP, 0x02390000)
        self.u.reg_write(UC_ARM_REG_LR, STOP | 1)
        self.u.emu_start(entry | 1, STOP, count=limit)
        assert self.u.reg_read(UC_ARM_REG_PC) == STOP, 'routine did not return'
        return self.u.reg_read(UC_ARM_REG_R0)


def images_from_rom(rom):
    from sovereign_editor import resident
    arm = arm9_code(rom)
    town = wr.town_overlay(rom)
    return arm, town['data'], cr.overlay(rom, 129)['data'], resident.boot_image(rom)


def images_from_plan(blob, plan):
    """Candidate ARM9/overlay images of a runtime plan before export (ARM9 patches applied)."""
    arm = bytearray(arm9_code(blob))
    arm_start, _, _, arm_size = struct.unpack_from('<4I', blob, 0x20)
    for p in plan['patches']:
        if arm_start <= p['rom_offset'] < arm_start + arm_size:
            at = p['rom_offset'] - arm_start
            before, after = bytes.fromhex(p['before']), bytes.fromhex(p['after'])
            assert arm[at:at + len(before)] == before, 'ARM9 patch before-value differs'
            arm[at:at + len(after)] = after
    town = wr.town_overlay(blob)
    ov101 = plan['files'].get(town['file_id'], town['data'])
    ov129 = plan['files'].get(cr.overlay(blob, 129)['file_id'], cr.overlay(blob, 129)['data'])
    from sovereign_editor import resident
    return bytes(arm), ov101, ov129, plan.get('appends', {}).get(resident.BOOT_ARCHIVE, [None])[0]


def static_cases(candidate, reference, members):
    """candidate/reference: (arm, ov101, ov129) images."""
    machine = Machine(candidate[0], ov129=candidate[2], boot=candidate[3])
    stock = Machine(reference[0], ov129=reference[2], boot=reference[3])
    rows, failures = [], 0
    top = max([700, *members]) + 2
    for member in range(top):
        got, ref = machine.call(wr.STATIC_CHECK, member), stock.call(wr.STATIC_CHECK, member)
        expected = 1 if member in members else ref
        ok = got == expected and (member in members or got == ref)
        failures += not ok
        if member in members or ref or not ok:
            rows.append({'member': member, 'candidate': got, 'stock': ref, 'pass': ok})
    return {'members_checked': top, 'excluded_stock': [r['member'] for r in rows if r['stock']],
            'excluded_created': sorted(members), 'failed': failures, 'rows': rows}


def town_cases(candidate, reference, stock_specs_count=wr.TOWN_STOCK_COUNT):
    def setup(m):
        app = bytearray(0x400)
        struct.pack_into('<I', app, SPECS_FIELD, wr.TOWN_SPECS)
        app[COUNT_FIELD] = stock_specs_count
        m.u.mem_write(APP, bytes(app))
        return m
    machine = setup(Machine(candidate[0], candidate[1], candidate[2], candidate[3]))
    stock = setup(Machine(reference[0], reference[1], reference[2], reference[3]))
    decoded = _created_specs(candidate)
    rows, failures, hits = [], 0, 0
    for x in range(GRID[0]):
        for y in range(GRID[1]):
            got, ref = machine.call(TOWN_ENTRY, APP, x, y), stock.call(TOWN_ENTRY, APP, x, y)
            cover = next((address for address, spec in decoded if spec[1] <= x < spec[1] + (spec[3] & 15)
                          and spec[2] <= y < spec[2] + (spec[3] >> 4 & 15)), 0)
            expected = ref if ref else cover
            ok = got == expected
            failures += not ok
            if cover or not ok:
                hits += bool(cover and got == cover)
                rows.append({'x': x, 'y': y, 'candidate': hex(got), 'stock': hex(ref), 'created': hex(cover), 'pass': ok})
    return {'cells_checked': GRID[0] * GRID[1], 'created_hits': hits, 'failed': failures, 'rows': rows,
            'specs': [{'address': hex(a), 'map_id': s[0], 'x': s[1], 'y': s[2], 'flavor': s[4]} for a, s in decoded]}


def _created_specs(images):
    ov101, ov129 = images[1], images[2]
    tail = wr.TOWN_TAIL - wr.TOWN_ADDRESS
    first, second = struct.unpack_from('<HH', ov101, tail)
    target = wr._bl_target(wr.TOWN_TAIL, first, second)
    if target is None:
        return []
    size = len(wr.town_hook(0, 0))
    offset = target - 0x023D8000
    table, count = struct.unpack_from('<2I', ov129, offset + size - 8)
    return [(table + 16 * k, wr.SPEC.unpack_from(ov129, table - 0x023D8000 + 16 * k)) for k in range(count)]


def _messages(rom, member):
    from sovereign_editor import dialogue_format as fmt, world_identity as wi
    from sovereign_editor.formats import resource
    raw = resource(rom, fmt.TEXT_ARCHIVE, member)[1]
    return fmt.text_entries(raw)[1], wi._stock_names(raw)


def readback(rom, project):
    """Independent readback of an exported candidate: every world-integration fact is decoded
    from the ROM bytes (ARM9 stubs, overlay 129/101, text and map archives) and compared with
    the Project's intended final state. The Project supplies expectations, not the decoding."""
    from sovereign_editor import world, world_identity as wi
    from sovereign_editor.formats import map_sections, resource
    state = project.composed()
    checks = []

    def add(name, ok, detail=None):
        checks.append({'check': name, 'pass': bool(ok), **({'detail': detail} if detail is not None else {})})
    base = project.blob
    ext = wr.decode_extension(rom)
    expected = [project._world_headers[h]['raw'] for h in sorted(project._world_headers)]
    add('resident header table decodes every created header', ext['present'] and ext['records'] == expected,
        {'version': ext.get('version'), 'count': ext.get('count'), 'overrides': len(ext.get('overrides', []))})
    base_arm = arm9_code(base)

    def needs_override(raw, template):
        try:
            wr.compact(base_arm, raw, template)
            return False
        except Exception:
            return True
    add('layout v3 exactly when a created header carries its own identity',
        ext.get('version') == (3 if any(needs_override(r, t) for r, t in _templates(project, state)) else 2))
    for h in sorted(project._world_headers):
        raw = ext['records'][h - wr.BASE_COUNT]
        decoded = world.decode_header(raw, h, '', None)
        ident = wi.identities(state).get(project._world_headers[h].get('identity'))
        if ident:
            add(f'header {h} identity fields', decoded['location_name'] == ident['section']
                and decoded['area_icon'] == ident['popup'] and decoded['weather'] == ident['weather']
                and (decoded['music_day'], decoded['music_night']) == (ident['music']['day'], ident['music']['night'])
                and decoded['kanto'] == (ident['region'] == 'kanto'), {k: decoded[k] for k in (
                    'location_name', 'area_icon', 'music_day', 'music_night', 'weather', 'kanto', 'worldmap')})
    for member, key in ((wi.SECTION_TEXT, 'sections'), (wi.DESCRIPTION_TEXT, 'descriptions')):
        wanted = state.get('world', {}).get(key, {})
        stock_entries, _ = _messages(base, member)
        entries, texts = _messages(rom, member)
        field = 'name' if key == 'sections' else 'text'
        add(f'msg {member}: stock messages keep their IDs and bytes',
            [e[2] for e in entries[:len(stock_entries)]] == [e[2] for e in stock_entries])
        add(f'msg {member}: appended {key} decode to the authored text',
            len(entries) == len(stock_entries) + len(wanted)
            and all(texts[i] == wanted[i][field] for i in wanted), {i: texts[i] for i in wanted})
    static = wr.decode_static(rom)
    add('animation exclusion list = authored static members', static['members'] == wi.static_list(state),
        static['members'])
    town = wr.decode_town(rom)
    specs = [wr.SPEC.unpack(s) for s in wi.town_specs(project, state)]
    add('Pokégear created location specs', town['specs'] == specs, [s[:5] for s in town['specs']])
    info_base, info_rom = wr.town_overlay(base), wr.town_overlay(rom)
    tail = wr.TOWN_TAIL - wr.TOWN_ADDRESS
    diff = [i for i in range(len(info_base['data'])) if info_base['data'][i] != info_rom['data'][i]]
    add('overlay 101 differs from stock only at the 4-byte lookup tail',
        (not specs and not diff) or (bool(diff) and min(diff) >= tail and max(diff) < tail + 4), diff[:8])
    add('overlay 101 stored with a consistent compression flag',
        info_rom['compressed'] == (not specs and info_base['compressed']))
    for key, area in sorted(wa_areas(state).items()):
        remaining = 0
        for cell in area['cells']:
            raw = resource(rom, world.MAP_ARCHIVE, cell['map_member'])[1]
            start = map_sections(raw)['permissions_offset']
            remaining += sum(raw[start + 2 * i] == 6 for i in range(world.MAP_SIZE * world.MAP_SIZE))
        expected_trees = wi.headbutt_tiles(project, state).get(key, 0)
        add(f'{key}: Headbutt-behavior tiles in exported maps', remaining == expected_trees, remaining)
    return {'checks': checks, 'passed': sum(c['pass'] for c in checks), 'failed': sum(not c['pass'] for c in checks)}


def wa_areas(state):
    from sovereign_editor import world_authoring
    return world_authoring.areas(state)


def _templates(project, state):
    return [(project._world_headers[a['header']]['raw'], a['template_header']) for a in wa_areas(state).values()]


def run(candidate_rom, baseline_rom):
    import world_qualification as wq
    candidate, reference = images_from_rom(candidate_rom), images_from_rom(baseline_rom)
    static = wr.decode_static(candidate_rom)
    report = {'candidate_sha256': digest(candidate_rom), 'baseline_sha256': digest(baseline_rom),
              'static': static_cases(candidate, reference, static['members']),
              'town': town_cases(candidate, reference),
              'headers': {k: v for k, v in wq.cpu(candidate_rom, baseline_rom).items() if k != 'rows'},
              'modeled': ['GF_AssertFail returns (retail)', 'Pokégear app struct zeroed except the spec pointer/count; '
                          'hidden-location flags therefore hide Safari/Sinjoh/S.S. Aqua in both runs']}
    report['failed'] = report['static']['failed'] + report['town']['failed'] + report['headers']['failed']
    return report


if __name__ == '__main__':
    result = run(Path(sys.argv[1]).read_bytes(), Path(sys.argv[2]).read_bytes())
    text = json.dumps(result, indent=1)
    if len(sys.argv) > 3:
        Path(sys.argv[3]).write_text(text + '\n')
    print(json.dumps({k: (v if not isinstance(v, dict) else {kk: vv for kk, vv in v.items() if kk != 'rows'})
                      for k, v in result.items()}, indent=1)[:3000])
