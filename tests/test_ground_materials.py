"""Custom ground materials, connected paving and ground decals (SURFACE-01..03). Software checks only."""
import contextlib
import io
import json
import struct
from pathlib import Path

import pytest
from PIL import Image

from sovereign_editor import border_authoring as ba, cli, ground_materials as gm, nitro, nitro_writer as nw, world
from sovereign_editor.core import Project
from sovereign_editor.formats import EditorError, resource

ROOT = Path(__file__).resolve().parents[1]
PARENT = ROOT / 'projects/editor-completion-v1'          # r82 parent, read-only here
pytestmark = pytest.mark.skipif(not PARENT.is_dir(), reason='r82 parent absent')
CHERRY = {'header': 67, 'cell': [17, 12]}


def indexed(path, w, h, fn, pal):
    im = Image.new('P', (w, h))
    im.putpalette(sum(pal, []) + [0] * (768 - 3 * len(pal)))
    im.putdata([fn(x, y) for y in range(h) for x in range(w)])
    im.save(path)


def material(folder, mid='tcob', shade=150, variants=True):
    folder.mkdir(parents=True, exist_ok=True)
    pal = [[0, 0, 0], [shade, shade, shade + 15], [120, 120, 135], [90, 90, 105], [112, 210, 170]]
    indexed(folder / 'fill.png', 64, 64, lambda x, y: 1 + ((x // 8 + y // 8) % 2), pal)
    indexed(folder / 'fill_cove.png', 64, 64, lambda x, y: 2 + ((x // 8 + y // 8) % 2), pal)
    indexed(folder / 'rim.png', 32, 32, lambda x, y: 4 if x < 8 else 1, pal)
    indexed(folder / 'rim_dirt.png', 32, 32, lambda x, y: 3 if x < 8 else 1, pal)
    indexed(folder / 'pile.png', 32, 32, lambda x, y: 0 if (x - 16) ** 2 + (y - 16) ** 2 > 200 else 1, pal)
    manifest = {'schema': gm.CONTRACT, 'id': mid, 'display': 'Test cobble', 'fill': 'fill.png',
                'rims': {'grass': 'rim.png', 'dirt': 'rim_dirt.png'}, 'decals': {'pile': {'texture': 'pile.png'}}}
    if variants:
        manifest['variants'] = {'cove': {'fill': 'fill_cove.png'}}
    (folder / 'material.json').write_text(json.dumps(manifest))
    return folder


def run_cli(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cli.main([str(a) for a in argv])
    return json.loads(out.getvalue())


PARENT_P = Project(PARENT) if PARENT.is_dir() else None


def region(p, mid='tcob'):
    return ba.custom_region(p, 67, p.composed(), mid)


def test_contract_refusals(tmp_path):
    good = material(tmp_path / 'good')
    source = gm.load_source(good)
    textures, report = gm.bake(source)
    assert sorted(textures) == ['base', 'cove'] and report['texture_bytes'] <= gm.MAX_TEXTURE_BYTES
    assert [t['name'] for t in textures['base']] == ['g_tcob_pile', 'g_tcob_f', 'g_tcob_d', 'g_tcob_r']
    assert all(len(gm.palette_name(t['name'])) <= 16 for t in textures['base'])
    cases = {'id too long': {'id': 'toolongid'}, 'unknown field': {'colour': 1}, 'bad rim role': {'rims': {'sand': 'rim.png'}},
             'bad variant role': {'variants': {'x': {'wall': 'rim.png'}}}}
    for name, change in cases.items():
        folder = material(tmp_path / name.replace(' ', '_'))
        manifest = json.loads((folder / 'material.json').read_text())
        manifest.update(change)
        (folder / 'material.json').write_text(json.dumps(manifest))
        with pytest.raises(EditorError):
            gm.bake(gm.load_source(folder))
    rgb = material(tmp_path / 'rgb')
    Image.new('RGB', (64, 64)).save(rgb / 'fill.png')
    with pytest.raises(EditorError):
        gm.bake(gm.load_source(rgb))
    wrong = material(tmp_path / 'wrong')
    indexed(wrong / 'rim.png', 16, 16, lambda x, y: 1, [[0, 0, 0], [1, 1, 1]])
    with pytest.raises(EditorError):
        gm.bake(gm.load_source(wrong))


def test_register_paint_extend_erase_variant_revise_and_export_plan(tmp_path):
    p = Project(PARENT).clone(tmp_path / 'p')
    src = material(tmp_path / 'src')
    # Import through the shared area-edit path (a CLI request naming its source folder).
    req = tmp_path / 'register.json'
    req.write_text(json.dumps({'operations': [{'kind': 'ground', 'context': CHERRY, 'request': {'source': str(src)}}],
                               'label': 'Register test cobble'}))
    dry = run_cli('area-edit', '--project', p.root, '--request', req, '--dry-run')
    assert dry['ok'] and [c['operation'] for c in dry['result']['preview']] == ['ground.transaction']
    done = run_cli('area-edit', '--project', p.root, '--request', req, '--revision', dry['result']['revision'])
    assert done['ok']
    p = Project(p.root)
    view = run_cli('ground-view', '--project', p.root)['result']
    assert view['materials']['tcob']['revision'] == 1 and view['shown_areas'] == []
    package = gm.package_dir(p, 'tcob', view['materials']['tcob']['package'])
    assert (package / 'manifest.json').is_file() and (package / 'source/material.json').is_file()
    # Paint a paved patch: 12 fill tiles and an 18-tile grass rim ring.
    paint = lambda tiles, **extra: {'kind': 'border', 'context': CHERRY, 'request': {
        'family': 'path', 'tiles': [{'x': x, 'z': z} for x, z in tiles], 'material': 'tcob', **extra}}
    first = [(x, z) for x in range(555, 559) for z in range(401, 404)]
    p.apply_area_edit(p.doc['revision'], operations=[paint(first)], label='Pave')
    filled, rims = region(p)
    assert filled == set(first) and len(rims) == 18
    # Ring tiles over stock dirt (road01) take the dirt rim, the rest the grass rim.
    dirt = {t for t in rims if ba.top_material(PARENT_P, 67, PARENT_P.composed(), *t) in ba.ROAD}
    assert dirt and {t for t, v in rims.items() if v == 'g_tcob_d'} == dirt
    # Extending joins the patches: the old east rim column becomes fill, the ring follows.
    p.apply_area_edit(p.doc['revision'], operations=[paint([(x, z) for x in range(559, 561) for z in range(401, 404)])],
                      label='Extend')
    filled, rims = region(p)
    assert filled == {(x, z) for x in range(555, 561) for z in range(401, 404)}
    assert not any(t in filled for t in rims) and len(rims) == 22
    # Erasing the west part leaves no rim behind on the old west edge.
    p.apply_area_edit(p.doc['revision'], operations=[paint([(x, z) for x in range(555, 557) for z in range(401, 404)],
                                                           erase=True)], label='Erase')
    filled, rims = region(p)
    assert filled == {(x, z) for x in range(557, 561) for z in range(401, 404)}
    assert min(x for x, _ in rims) == 556 and not any(x == 554 for x, _ in rims)
    ctx = p.context(**CHERRY)
    tex, _ = nitro.texture_set(gm.editor_tileset(p, p.composed(), 2, resource(p.blob, world.MAP_TEXTURE_ARCHIVE, 2)[1]))
    _, prims = nitro.decode_model(__import__('sovereign_editor.surface_authoring', fromlist=['model']).model(
        p, ctx, p.composed()), tileset=gm.editor_tileset(p, p.composed(), 2, resource(p.blob, world.MAP_TEXTURE_ARCHIVE, 2)[1]),
        render=False)
    assert {'g_tcob_f', 'g_tcob_r'} <= {q.material['texture_name'] for q in prims}
    # Export plan: one superset tileset for area data 2, stock texels unchanged, word rebinding.
    plan = gm.export_plan(p, p.composed())
    [row] = plan['report']['areas']
    assert row['area_data'] == 2 and row['texture_vram_bytes'] <= row['stock_outdoor_maximum']
    stock_tex, stock_pal = nitro.texture_set(resource(p.blob, world.MAP_TEXTURE_ARCHIVE, 2)[1])
    new_tex, new_pal = nitro.texture_set(plan['appends'][world.MAP_TEXTURE_ARCHIVE][0])
    assert all((new_tex[n].data1, new_tex[n].data2) == (stock_tex[n].data1, stock_tex[n].data2) for n in stock_tex)
    assert all(new_pal[n] == stock_pal[n] for n in stock_pal)
    area = plan['replacements'][gm.AREA_ARCHIVE][2]
    assert struct.unpack_from('<H', area, 2)[0] == row['created_map_tileset']
    assert area[4:] == resource(p.blob, gm.AREA_ARCHIVE, 2)[1][4:]
    # Area-scoped variant: area data 2 resolves the same fill name to the cove texels.
    base_fill = next(t for t in gm.area_textures(p, p.composed(), 2) if t['role'] == 'fill')['indices']
    p.apply_area_edit(p.doc['revision'], operations=[{'kind': 'ground', 'context': CHERRY, 'request': {
        'action': 'variant', 'material': 'tcob', 'area_data': 2, 'variant': 'cove'}}], label='Cove variant')
    cove_fill = next(t for t in gm.area_textures(p, p.composed(), 2) if t['role'] == 'fill')['indices']
    other_fill = next(t for t in gm.area_textures(p, p.composed(), 0) if t['role'] == 'fill')['indices']
    assert cove_fill != base_fill and other_fill == base_fill
    with pytest.raises(EditorError):
        p.plan_area_edit([{'kind': 'ground', 'context': CHERRY, 'request': {
            'action': 'variant', 'material': 'tcob', 'area_data': 2, 'variant': 'nope'}}])
    # Reimport: a changed fill revises in place; the impact names the painted map; stale refused.
    revised = material(tmp_path / 'rev', shade=170)
    stage = p.stage_ground_source(revised)
    assert stage['operation']['action'] == 'revise' and stage['users'] and not stage['unchanged']
    p.apply_area_edit(p.doc['revision'], operations=[{'kind': 'ground', 'context': CHERRY, 'request': stage['operation']}],
                      label='Revise cobble')
    assert p.ground_view()['materials']['tcob']['revision'] == 2
    with pytest.raises(EditorError) as stale:
        p.plan_area_edit([{'kind': 'ground', 'context': CHERRY, 'request': stage['operation']}])
    assert stale.value.code in ('STALE_ASSET', 'NO_CHANGE')
    # A revision may not drop a role in use.
    dropped = material(tmp_path / 'drop', shade=180)
    manifest = json.loads((dropped / 'material.json').read_text())
    manifest['rims'] = {'dirt': 'rim_dirt.png'}
    (dropped / 'material.json').write_text(json.dumps(manifest))
    with pytest.raises(EditorError) as dep:
        op = p.stage_ground_source(dropped)['operation']
        p.plan_area_edit([{'kind': 'ground', 'context': CHERRY, 'request': op}])
    assert dep.value.code in ('DEPENDENCY', 'UNSUPPORTED_BORDER')
    # Undo/redo and reopen from disk.
    rev = p.doc['revision']
    p.undo(rev)
    assert p.ground_view()['materials']['tcob']['revision'] == 1
    p.redo(p.doc['revision'])
    again = Project(p.root)
    assert again.ground_view()['materials']['tcob']['revision'] == 2 and region(again)[0] == region(p)[0]
    # A tampered package refuses to open.
    body = gm.package_dir(again, 'tcob', again.ground_view()['materials']['tcob']['package'])
    (body / 'baked/textures.json').write_bytes(b'[]')
    with pytest.raises(EditorError):
        Project(p.root)


def test_decals_place_move_duplicate_erase_revise_without_collision(tmp_path):
    p = Project(PARENT).clone(tmp_path / 'p')
    src = material(tmp_path / 'src', variants=False)
    p.apply_area_edit(p.doc['revision'], operations=[{'kind': 'ground', 'context': CHERRY, 'request': {'source': str(src)}}],
                      label='Register')
    permissions = dict(p.composed()['permissions'])
    decal = lambda **r: {'kind': 'decal', 'context': CHERRY, 'request': r}
    p.apply_area_edit(p.doc['revision'], operations=[
        decal(action='place', key='pile_a', material='tcob', decal='pile', x=553.0, z=398.0),
        decal(action='place', key='pile_b', material='tcob', decal='pile', x=559.5, z=402.5, rotation=90, layer=1)],
        label='Decals')
    member = p.context(**CHERRY)['map_member']
    assert sorted(p.composed()['ground_decals'][member]) == ['pile_a', 'pile_b']
    assert p.composed()['permissions'] == permissions                # decals never block
    p.apply_area_edit(p.doc['revision'], operations=[decal(action='duplicate', key='pile_c', source='pile_a', x=551, z=396)],
                      label='dup')
    p.apply_area_edit(p.doc['revision'], operations=[decal(action='move', key='pile_c', x=550.5)], label='move')
    p.apply_area_edit(p.doc['revision'], operations=[decal(action='revise', key='pile_c', rotation=180, flip=True)],
                      label='revise')
    c = p.composed()['ground_decals'][member]['pile_c']
    assert (c['x'], c['z'], c['rotation'], c['flip']) == (550.5, 396.0, 180, True)
    p.apply_area_edit(p.doc['revision'], operations=[decal(action='erase', key='pile_c')], label='erase')
    assert sorted(p.composed()['ground_decals'][member]) == ['pile_a', 'pile_b']
    for bad in (decal(action='erase', key='nope'), decal(action='place', key='pile_a', material='tcob', decal='pile', x=1, z=1),
                decal(action='place', key='x', material='tcob', decal='none', x=553, z=398),
                decal(action='place', key='y', material='tcob', decal='pile', x=100.0, z=100.0),
                decal(action='move', key='pile_a', x=553.0, z=398.0),
                decal(action='place', key='z', material='tcob', decal='pile', x=553.01, z=398.0)):
        with pytest.raises(EditorError):
            p.plan_area_edit([bad])
    ctx = p.context(**CHERRY)
    from sovereign_editor import surface_authoring
    tileset = gm.editor_tileset(p, p.composed(), 2, resource(p.blob, world.MAP_TEXTURE_ARCHIVE, 2)[1])
    _, prims = nitro.decode_model(surface_authoring.model(p, ctx, p.composed()), tileset=tileset, render=False)
    piles = [q for q in prims if q.material['texture_name'] == 'g_tcob_pile']
    assert piles and all(len(q.vertices) for q in piles)
    ground = min(float(v[1]) for q in piles for v in q.vertices)
    assert ground >= 1.0 - 1e-6                                       # at least one unit over the ground plane
    p.undo(p.doc['revision'])
    assert sorted(p.composed()['ground_decals'][member]) == ['pile_a', 'pile_b', 'pile_c']
    assert Project(p.root).doc['revision'] == p.doc['revision']


def test_raised_street_paving_and_decals_on_a_terrace_top(tmp_path):
    """A terrace top paves as a raised street: the paving ring stops at the cliff rim/stairs, a
    stair landing takes the fill without a behavior change, and decals lie on the top but never
    across the rim."""
    from sovereign_editor import terrain_authoring as ta
    p = Project(PARENT).clone(tmp_path / 'p')
    src = material(tmp_path / 'src', variants=False)
    town = {'header': 67, 'cell': [17, 12]}
    p.apply_area_edit(p.doc['revision'], operations=[
        {'kind': 'ground', 'context': town, 'request': {'source': str(src)}},
        {'kind': 'world', 'context': town, 'request': {
            'action': 'create', 'identity': 'tst_town', 'name': 'Test Town', 'internal_name': 'TST_TOWN',
            'template_header': 67, 'encounters': 'none', 'worldmap': [17, 12],
            'cells': [{'cell': [0, 0], 'source': {'header': 67, 'cell': [17, 12]}}]}},
        {'kind': 'world', 'context': {'header': 72, 'cell': [0, 0]}, 'request': {
            'action': 'create', 'identity': 'tst_room', 'name': 'Test Room', 'internal_name': 'TST_ROOM',
            'template_header': 72, 'cells': [{'cell': [0, 0], 'source': {'header': 72, 'cell': [0, 0]}}],
            'encounters': 'none', 'worldmap': [17, 12], 'close': True}}], label='Area')
    header = max(a['header'] for a in __import__('sovereign_editor.world_authoring', fromlist=['areas'])
                 .areas(p.composed()).values() if a['identity'] == 'tst_town')
    ctx = {'header': header, 'cell': [0, 0]}
    terrace = {'action': 'terrace', 'x': 7, 'z': 18, 'width': 5, 'height': 3, 'access': [{'side': 'north', 'offset': 1}],
               'label': 'Raised street'}
    p.apply_area_edit(p.doc['revision'], operations=[
        {'kind': 'world', 'context': ctx, 'request': {'action': 'connect', 'x': 3, 'z': 15,
                                                      'destination': {'header': header + 1}, 'arrival': {'x': 4, 'z': 8}}},
        {'kind': 'elevation', 'context': ctx, 'request': terrace}], label='Terrace')
    lay = ta.layout(ta.normalise(terrace))
    top, landings = set(lay['top']), set(lay['landings'])
    edges = set(lay['footprint']) - top
    lower = {(x, 16) for x in range(4, 14)}                     # the street below, through the landing
    assert landings <= lower
    permissions = dict(p.composed()['permissions'])
    paint = lambda tiles: {'kind': 'border', 'context': ctx, 'request': {
        'family': 'path', 'tiles': [{'x': x, 'z': z} for x, z in sorted(tiles)], 'material': 'tcob'}}
    p.apply_area_edit(p.doc['revision'], operations=[paint(top), paint(lower)], label='Pave')
    filled, rims = ba.custom_region(p, header, p.composed(), 'tcob')
    assert filled == top | lower and not set(rims) & edges      # no rim painted over the cliff or stairs
    member = p.context(**ctx)['map_member']
    changed = {k for k, v in p.composed()['permissions'].items() if permissions.get(k) != v}
    offsets = {world.cell_offset(p.context(**ctx), x, z) for x, z in top | landings}
    assert not {off for m, off in changed if m == member} & offsets   # tops/landings keep terrace behavior
    decal = lambda key, x, z: {'kind': 'decal', 'context': ctx, 'request': {
        'action': 'place', 'key': key, 'material': 'tcob', 'decal': 'pile', 'x': x, 'z': z}}
    p.apply_area_edit(p.doc['revision'], operations=[decal('top', 9.5, 19.5)], label='Pile on top')
    with pytest.raises(EditorError) as owned:
        p.plan_area_edit([decal('rim', 6.0, 17.5)])
    assert owned.value.code == 'TERRAIN_OWNED'


def test_repaint_keeps_the_last_stock_decal_of_a_material(tmp_path):
    """Covering a map's only stock flower decal keeps it (an empty model shape is not encodable)."""
    from sovereign_editor import surface_authoring
    p = Project(PARENT).clone(tmp_path / 'p')
    src = material(tmp_path / 'src', variants=False)
    p.apply_area_edit(p.doc['revision'], operations=[{'kind': 'ground', 'context': CHERRY, 'request': {'source': str(src)}}],
                      label='Register')
    ctx = p.context(**CHERRY)
    tileset = lambda: gm.editor_tileset(p, p.composed(), 2, resource(p.blob, world.MAP_TEXTURE_ARCHIVE, 2)[1])
    flowers = lambda: [q for q in nitro.decode_model(surface_authoring.model(p, ctx, p.composed()), tileset=tileset(),
                                                     render=False)[1] if q.material['texture_name'] == 'flower02']
    before = flowers()
    assert len(before) == 1
    tiles = {(int((v[0] + 256) // 16) + 544, int((v[2] + 256) // 16) + 384) for v in before[0].vertices}
    # Fill its north row; the paving ring covers the rest (its south row is the tree's collision).
    tiles = {t for t in tiles if t[1] == min(z for _, z in tiles)}
    p.apply_area_edit(p.doc['revision'], operations=[{'kind': 'border', 'context': CHERRY, 'request': {
        'family': 'path', 'tiles': [{'x': x, 'z': z} for x, z in sorted(tiles)], 'material': 'tcob'}}], label='Pave')
    after = flowers()
    assert len(after) == 1 and len(after[0].triangles) == len(before[0].triangles)
