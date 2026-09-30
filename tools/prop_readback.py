"""Independent readback of custom-prop resources in an exported ROM (evidence only).

Reads the exported ROM with the generic format readers (not the export code path) and
checks, against the project's composed state and the pinned baseline:
model members 340.. equal the baked packages; attribute records equal the static donor;
created building sets keep every stock texture/palette byte and name and add the textures of
the assets their areas show; created build lists = stock list + those model IDs; only the rebound area data
changed; bm_field_matshp.dat covers the new IDs with draw-normal entries; each placed
instance has a map record with its model/position and blocked collision cells; each
custom model decodes against the ROM's created set with every material textured.

    prop_readback.py PROJECT ROM [OUT.json]
"""
import json
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sovereign_editor import nitro, nitro_writer as nw, props, world  # noqa: E402
from sovereign_editor.formats import file_span, map_data, member_count, resource, digest  # noqa: E402


def textures_of(btx0):
    at = struct.unpack_from('<I', btx0, 16)[0]
    t0 = btx0[at:]
    h = nw._tex0_header(t0)
    tex, _ = nw.read_dictionary(t0, h['tex_dict'])
    pal, _ = nw.read_dictionary(t0, h['pal_dict'])
    out = {}
    for name, entry in tex:
        param, extra = struct.unpack('<II', entry)
        fmt = (param >> 26) & 7; w = 8 << ((param >> 20) & 7); hh = 8 << ((param >> 23) & 7)
        bits = {1: 8, 2: 2, 3: 4, 4: 8, 5: 2, 6: 8, 7: 16}[fmt]
        start = h['tex_data'] + (param & 0xffff) * 8
        out[name] = {'param': param & ~0xffff, 'extra': extra, 'texels': t0[start:start + w * hh * bits // 8]}
    pals = {}
    for name, entry in pal:
        off = struct.unpack_from('<H', entry)[0] * 8
        pals[name] = t0[h['pal_data'] + off:h['pal_data'] + off + 32]
    return out, pals


def run(project, rom):
    checks = []

    def check(label, ok, detail=None):
        checks.append({'check': label, 'pass': bool(ok), **({'detail': detail} if detail is not None else {})})
    state = project.composed()
    registry = props.assets(state)
    plan = props.export_plan(project, state)
    base = project.blob
    ordered = sorted(registry.items(), key=lambda kv: kv[1]['model_id'])
    check('model archive = stock + registered assets', member_count(rom, props.MODEL_ARCHIVE) == 340 + len(ordered))
    for aid, a in ordered:
        model, textures = props.baked(project, aid, a['package'])
        check(f'{aid}: model member {a["model_id"]} equals its package', resource(rom, props.MODEL_ARCHIVE, a['model_id'])[1] == model)
        check(f'{aid}: attribute record is the static donor', resource(rom, props.ATTR_ARCHIVE, a['model_id'])[1]
              == resource(base, props.ATTR_ARCHIVE, props.STATIC_ATTRIBUTE_DONOR)[1])
    check('stock model and attribute members unchanged',
          all(resource(rom, props.MODEL_ARCHIVE, i)[1] == resource(base, props.MODEL_ARCHIVE, i)[1] for i in range(340))
          and all(resource(rom, props.ATTR_ARCHIVE, i)[1] == resource(base, props.ATTR_ARCHIVE, i)[1] for i in range(340)))
    stock_sets = member_count(base, props.TILESET_ARCHIVE)
    shown = props.set_assets(project, state)
    for stock, created in plan['report']['created_sets'].items():
        stock = int(stock)
        new_tex, new_pal = textures_of(resource(rom, props.TILESET_ARCHIVE, created)[1])
        old_tex, old_pal = textures_of(resource(base, props.TILESET_ARCHIVE, stock)[1])
        same = all(n in new_tex and new_tex[n]['texels'] == v['texels'] and new_tex[n]['param'] == v['param']
                   and new_tex[n]['extra'] == v['extra'] for n, v in old_tex.items())
        pals = all(new_pal.get(n) == v for n, v in old_pal.items())
        check(f'created set {created}: every stock texture of set {stock} byte-identical (texels, format, size)', same)
        check(f'created set {created}: every stock palette identical', pals)
        wanted = {t['name'] for aid in shown[stock] for t in props.baked(project, aid, registry[aid]['package'])[1]}
        check(f'created set {created}: asset textures present', wanted <= set(new_tex), sorted(wanted))
        lst = resource(rom, props.BUILD_LIST_ARCHIVE, created)[1]
        old = resource(base, props.BUILD_LIST_ARCHIVE, stock)[1]
        n, m = struct.unpack_from('<H', lst)[0], struct.unpack_from('<H', old)[0]
        ids = list(struct.unpack_from(f'<{n}H', lst, 2))
        check(f'created build list {created} = stock list {stock} + asset models',
              ids == list(struct.unpack_from(f'<{m}H', old, 2)) + [registry[aid]['model_id'] for aid in shown[stock]] and n < 0x226)
    check('stock building sets and lists unchanged', all(resource(rom, props.TILESET_ARCHIVE, i)[1] == resource(base, props.TILESET_ARCHIVE, i)[1]
                                                          and resource(rom, props.BUILD_LIST_ARCHIVE, i)[1] == resource(base, props.BUILD_LIST_ARCHIVE, i)[1]
                                                          for i in range(stock_sets)))
    changed = [i for i in range(member_count(base, props.AREA_ARCHIVE)) if resource(rom, props.AREA_ARCHIVE, i)[1] != resource(base, props.AREA_ARCHIVE, i)[1]]
    # Ground materials rebind the map-tileset word (bytes 2..3) of the areas that show them.
    from sovereign_editor import ground_materials as gm
    ground = gm.export_plan(project, state)
    maps = {r['area_data']: r['created_map_tileset'] for r in (ground['report']['areas'] if ground else [])}
    sets = {int(k): v for k, v in plan['report']['area_rebinding'].items()}
    word = lambda raw, at: struct.unpack_from('<H', raw, at)[0]
    check('only rebound area data changed: the building-set word (props) and map-tileset word (ground materials)',
          changed == sorted(set(sets) | set(maps))
          and all(resource(rom, props.AREA_ARCHIVE, i)[1][4:] == resource(base, props.AREA_ARCHIVE, i)[1][4:]
                  and word(resource(rom, props.AREA_ARCHIVE, i)[1], 0) == sets.get(i, word(resource(base, props.AREA_ARCHIVE, i)[1], 0))
                  and word(resource(rom, props.AREA_ARCHIVE, i)[1], 2) == maps.get(i, word(resource(base, props.AREA_ARCHIVE, i)[1], 2))
                  for i in changed), changed)
    _, mat = file_span(rom, props.MATSHP); _, old = file_span(base, props.MATSHP)
    count, pairs = struct.unpack_from('<2H', mat)
    check('bm_field_matshp.dat covers every model ID with normal draw for the new ones',
          count == 340 + len(ordered) and mat[4:4 + 4 * 340] == old[4:4 + 4 * 340]
          and mat[4 + 4 * 340:4 + 4 * count] == bytes(4 * len(ordered)) and mat[4 + 4 * count:] == old[4 + 4 * 340:])
    placed = props.instances(state)
    for key, inst in sorted(placed.items()):
        ctx = project.context(header=inst['context']['header'], cell=inst['context']['cell'])
        raw = resource(rom, world.MAP_ARCHIVE, ctx['map_member'])[1]
        collision, records, _ = map_data(raw)
        want = registry[inst['asset']]['model_id']
        words = {'x': world.record_value(inst['x'] - ctx['origin'][0] - 16), 'z': world.record_value(inst['z'] - ctx['origin'][1] - 16)}
        hit = [r for r in records if r['model_id'] == want and r['xyz_raw'][0] == words['x'] and r['xyz_raw'][2] == words['z']]
        check(f'{key}: map member {ctx["map_member"]} has its record', len(hit) == 1)
        blocked = all(world.is_blocked(raw[world.cell_offset(ctx, x, z):world.cell_offset(ctx, x, z) + 2]) for x, z in inst['cells'])
        check(f'{key}: collision cells blocked in the exported map', blocked, inst['cells'])
        check(f'{key}: map keeps <= 32 objects', len(records) <= 32, len(records))
    # Each created set carries the assets its areas show; every shown model must decode textured.
    for stock, created in plan['report']['created_sets'].items():
        for aid in shown[int(stock)]:
            summary, prims = nitro.decode_model(resource(rom, props.MODEL_ARCHIVE, registry[aid]['model_id'])[1],
                                                tileset=resource(rom, props.TILESET_ARCHIVE, created)[1])
            check(f'{aid}: decodes from the ROM with every material textured by created set {created}',
                  summary['textured_materials'] == len(summary['materials']), summary['texture_source'])
    return {'scope': 'independent format readback of the exported ROM; not native acceptance',
            'rom_sha256': digest(rom), 'checks': checks, 'passed': sum(c['pass'] for c in checks),
            'failed': sum(not c['pass'] for c in checks)}


if __name__ == '__main__':
    from sovereign_editor.core import Project
    report = run(Project(sys.argv[1]), Path(sys.argv[2]).read_bytes())
    if len(sys.argv) > 3:
        Path(sys.argv[3]).write_text(json.dumps(report, indent=1) + '\n')
    print(json.dumps({'passed': report['passed'], 'failed': report['failed']}))
    for c in report['checks']:
        if not c['pass']:
            print('FAIL', c)
