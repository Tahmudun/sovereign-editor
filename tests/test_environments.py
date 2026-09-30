"""Reusable named environments (KIT-02): define, place presets in created areas, revise, retire,
reopen, undo and reuse into a clean project. Software checks only."""
import copy
from pathlib import Path

import pytest

from sovereign_editor import border_authoring as ba, environments as envs, props, reuse, world, world_authoring as wa
from sovereign_editor.core import Project
from sovereign_editor.formats import EditorError

ROOT = Path(__file__).resolve().parents[1]
PARENT = ROOT / 'projects/editor-completion-v1'
ART = ROOT / 'work/original-content-v1/art'
pytestmark = pytest.mark.skipif(not (PARENT.is_dir() and ART.is_dir()), reason='r82 parent or kit art absent')
TOWN = {'header': 67, 'cell': [17, 12]}
BED = [[dx, dz] for dz in range(4) for dx in range(3)]
TOWN_ENV = {'display': 'Test town', 'kind': 'outdoor', 'ground': ['cobble', 'petals'], 'props': ['blossom', 'bed'],
            'presets': {
                'raised_street': {'type': 'terrace', 'width': 5, 'height': 3,
                                  'access': [{'side': 'north', 'offset': 1}], 'pave': 'cobble'},
                'grove': {'type': 'props', 'items': [{'asset': 'blossom', 'dx': 0.5, 'dz': 0.5, 'collision': [[0, 0]]},
                                                     {'asset': 'blossom', 'dx': 3.5, 'dz': 0.5, 'collision': [[0, 0]]}]},
                'bed': {'type': 'props', 'items': [{'asset': 'bed', 'dx': 0.5, 'dz': 0.5, 'collision': BED}]},
                'clearing': {'type': 'decals', 'items': [{'material': 'petals', 'decal': 'pilem', 'dx': 1.0, 'dz': 1.0},
                                                         {'material': 'petals', 'decal': 'sc1', 'dx': 2.5, 'dz': 1.5}]}}}
COAST_ENV = {'display': 'Cove', 'kind': 'coast', 'donors': {'cove': {'header': 67, 'cell': [16, 12]}},
             'presets': {'cove': {'type': 'area', 'donor': 'cove'}}}
# Ridge Cave's three rooms (r82), relative to the anchor (14, 3).
CAVE_ENV = {'display': 'Ridge cave', 'kind': 'cave', 'presets': {'hall': {
    'type': 'cave_room', 'rects': [[0, 18, 15, 8], [3, 0, 9, 9], [6, 9, 2, 9]],
    'exits': [{'dx': 7, 'dz': 25}, {'dx': 12, 'dz': 25}], 'encounters': True}}}


def edit(p, *ops, label='Edit'):
    return p.apply_area_edit(p.doc['revision'], operations=list(ops), label=label)


def env_op(ctx, **request):
    return {'kind': 'environment', 'context': ctx, 'request': request}


@pytest.fixture(scope='module')
def base(tmp_path_factory):
    root = tmp_path_factory.mktemp('env') / 'p'
    p = Project(PARENT).clone(root)
    edit(p, {'kind': 'ground', 'context': TOWN, 'request': {'source': str(ART / 'cherrygrove-kit/src/cobble')}},
         {'kind': 'ground', 'context': TOWN, 'request': {'source': str(ART / 'cherrygrove-kit/src/petals')}},
         {'kind': 'prop', 'context': TOWN, 'request': {'source': str(ART / 'garden-kit/src/bed')}},
         {'kind': 'world', 'context': TOWN, 'request': {
             'action': 'create', 'identity': 'tst_town', 'name': 'Test Town', 'internal_name': 'TST_TOWN',
             'template_header': 67, 'encounters': 'none', 'worldmap': [17, 12], 'cells': [{'cell': [0, 0], 'source': TOWN}]}},
         {'kind': 'world', 'context': {'header': 72, 'cell': [0, 0]}, 'request': {
             'action': 'create', 'identity': 'tst_room', 'name': 'Test Room', 'internal_name': 'TST_ROOM',
             'template_header': 72, 'cells': [{'cell': [0, 0], 'source': {'header': 72, 'cell': [0, 0]}}],
             'encounters': 'none', 'worldmap': [17, 12], 'close': True}},
         {'kind': 'world', 'context': {'header': 461, 'cell': [0, 0]}, 'request': {
             'action': 'create', 'identity': 'tst_cave', 'name': 'Test Cave', 'internal_name': 'TST_CAVE',
             'template_header': 461, 'cells': [{'cell': [0, 0], 'source': {'header': 461, 'cell': [0, 0]}}],
             'encounters': 'template', 'worldmap': [19, 12], 'close': True}}, label='Fixture')
    headers = {a['identity']: a['header'] for a in wa.areas(p.composed()).values()}
    edit(p, {'kind': 'world', 'context': {'header': headers['tst_town'], 'cell': [0, 0]}, 'request': {
        'action': 'connect', 'x': 3, 'z': 15, 'destination': {'header': headers['tst_room']}, 'arrival': {'x': 4, 'z': 8}}},
         label='Door')
    return root, headers


@pytest.fixture
def project(base, tmp_path):
    root, headers = base
    return Project(root).clone(tmp_path / 'p'), headers


def blocked(p, ctx, x, z):
    at = world.cell_offset(ctx, x, z)
    return p.composed()['permissions'].get((ctx['map_member'], at), p.member_raw(ctx['map_member'])[at:at + 2])[1] & 0x80


def test_define_place_revise_retire_reopen_and_undo(project):
    p, h = project
    ctx = {'header': h['tst_town'], 'cell': [0, 0]}
    edit(p, env_op(TOWN, action='define', key='town', **TOWN_ENV), env_op(TOWN, action='define', key='coast', **COAST_ENV),
         env_op(TOWN, action='define', key='cave', **CAVE_ENV), label='Environments')
    view = p.environment_view()['environments']
    assert view['town']['revision'] == 1 and view['town']['dependencies']['ground'].keys() == {'cobble', 'petals'}
    assert {n: v['type'] for n, v in view['town']['presets'].items()} == {
        'bed': 'props', 'clearing': 'decals', 'grove': 'props', 'raised_street': 'terrace'}
    # One placement = one area edit = one undo; it expands into the ordinary operations.
    before = p.doc['revision']
    plan = p.plan_area_edit([env_op(ctx, action='place', environment='town', preset='raised_street', x=7, z=18)])
    assert [c['operation'] for c in plan['preview']] == ['terrain.feature', 'map.transaction', 'surface.transaction']
    edit(p, env_op(ctx, action='place', environment='town', preset='raised_street', x=7, z=18),
         env_op(ctx, action='place', environment='town', preset='clearing', x=7, z=18, key='clr'),
         env_op(ctx, action='place', environment='town', preset='grove', x=14, z=19, key='gr'),
         env_op(ctx, action='place', environment='town', preset='bed', x=19, z=13, key='bd'), label='Place kit')
    assert p.doc['revision'] == before + 1
    state = p.composed()
    top = {(x, z) for x in range(7, 12) for z in range(18, 21)}
    assert top <= ba.custom_region(p, h['tst_town'], state, 'cobble')[0]              # paved raised street
    placed = props.instances(state)
    assert (placed['gr0']['x'], placed['gr1']['x'], placed['bd0']['asset']) == (14.5, 17.5, 'bed')
    cell = p.context(**ctx)
    assert sorted(state['ground_decals'][cell['map_member']]) == ['clr_0', 'clr_1']
    assert all(blocked(p, cell, 19 + dx, 13 + dz) for dx, dz in BED)
    # Second context: the cave preset in another created area; the coast preset creates an area from its donor.
    cave = {'header': h['tst_cave'], 'cell': [0, 0]}
    edit(p, env_op(cave, action='place', environment='cave', preset='hall', x=14, z=3),
         env_op(ctx, action='place', environment='coast', preset='cove', identity='tst_cove', name='Test Cove',
                internal_name='TST_COVE'), label='Second contexts')
    features = [i['spec'] for items in p.composed()['terrain_features'].values() for i in items
                if i['header'] == h['tst_cave']]
    assert features[0]['rects'][0] == [14, 21, 15, 8] and features[0]['exits'][0] == {'x': 21, 'z': 28}
    assert 'tst_cove' in {a['identity'] for a in wa.areas(p.composed()).values()}
    # Revise with the current revision; a stale revision is refused; retire; retired cannot be placed.
    revised = copy.deepcopy(TOWN_ENV)
    revised['presets']['pond'] = {'type': 'pond', 'width': 3, 'height': 2, 'traversable': False}
    edit(p, env_op(TOWN, action='revise', key='town', expected_revision=1, presets=revised['presets']))
    assert p.environment_view()['environments']['town']['revision'] == 2
    with pytest.raises(EditorError) as stale:
        p.plan_area_edit([env_op(TOWN, action='revise', key='town', expected_revision=1, display='Old')])
    assert stale.value.code == 'STALE_ASSET'
    edit(p, env_op(TOWN, action='retire', key='coast', expected_revision=1))
    with pytest.raises(EditorError) as retired:
        p.plan_area_edit([env_op(ctx, action='place', environment='coast', preset='cove', identity='x2', name='X',
                                 internal_name='X2')])
    assert retired.value.code == 'RETIRED'
    reopened = Project(p.root)
    assert reopened.environment_view() == p.environment_view()
    p.undo(p.doc['revision'])                                               # the retirement
    p.undo(p.doc['revision'])                                               # the revision
    assert p.environment_view()['environments']['town']['revision'] == 1


def test_refusals_and_resolved_group_presets(project):
    p, h = project
    bad = [dict(TOWN_ENV, ground=['nope']),
           dict(TOWN_ENV, presets={'x': {'type': 'terrace', 'width': 20, 'height': 3}}),
           dict(TOWN_ENV, presets={'x': {'type': 'props', 'items': [{'asset': 'lantern', 'dx': 0.5, 'dz': 0.5,
                                                                      'collision': []}]}}),
           dict(TOWN_ENV, presets={'x': {'type': 'props', 'items': [{'asset': 'blossom', 'dx': 1.0, 'dz': 0.5,
                                                                      'collision': []}]}}),
           dict(TOWN_ENV, presets={'x': {'type': 'decals', 'items': [{'material': 'petals', 'decal': 'nope',
                                                                       'dx': 0, 'dz': 0}]}}),
           dict(COAST_ENV, donors={'cove': {'header': h['tst_town'], 'cell': [0, 0]}})]    # donors are stock cells
    for spec in bad:
        with pytest.raises(EditorError):
            p.plan_area_edit([env_op(TOWN, action='define', key='bad', **spec)])
    # A prop-only group template becomes items (stored resolved, so the environment travels as data).
    edit(p, {'kind': 'map_group', 'context': TOWN, 'request': {'action': 'capture', 'name': 'pair', 'anchor': {'x': 559, 'z': 393},
                                                               'props': ['tree_north', 'tree_sw']}})
    edit(p, env_op(TOWN, action='define', key='grove', display='Grove', kind='outdoor', props=['blossom'],
                   presets={'pair': {'type': 'props', 'from_group': 'pair'}}))
    items = envs.catalog(p.composed())['grove']['presets']['pair']['items']
    assert [(i['dx'], i['dz']) for i in items] == [(0.5, 0.5), (-10.5, 12.5)]
    with pytest.raises(EditorError) as mixed:
        edit(p, {'kind': 'map_group', 'context': TOWN, 'request': {'action': 'capture', 'name': 'signed', 'anchor': {
            'x': 559, 'z': 393}, 'props': ['tree_north'], 'interactions': ['simple:28']}})
        p.plan_area_edit([env_op(TOWN, action='define', key='g2', display='G', kind='outdoor', props=['blossom'],
                                 presets={'p': {'type': 'props', 'from_group': 'signed'}})])
    assert mixed.value.code == 'UNSUPPORTED_GROUP'


def test_reuse_into_a_clean_project_brings_members(project, tmp_path):
    p, _ = project
    edit(p, env_op(TOWN, action='define', key='town', **TOWN_ENV))
    clean = Project.create(PARENT / 'baseline.nds', tmp_path / 'clean', name='Clean')
    plan = reuse.plan(clean, p, ['environment:town'])
    assert [(r['kind'], r['key']) for r in plan['items']] == [
        ('prop', 'blossom'), ('prop', 'bed'), ('ground', 'cobble'), ('ground', 'petals'), ('environment', 'town')]
    reuse.apply(clean, p, ['environment:town'], clean.doc['revision'])
    assert clean.environment_view()['environments']['town']['presets'] == p.environment_view()['environments']['town']['presets']


def test_world_editor_environments_tab(project):
    import os
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtWidgets import QApplication
    from sovereign_editor.map_inspector import MapInspectorWindow
    from sovereign_editor.world_ui import WorldEditor
    p, h = project
    edit(p, env_op(TOWN, action='define', key='town', **TOWN_ENV))
    app = QApplication.instance() or QApplication([])
    w = MapInspectorWindow(p, context=(h['tst_town'], [0, 0])); w.show(); app.processEvents()
    d = WorldEditor(w); d.tabs.setCurrentIndex(d.tabs.indexOf(d.env_list.parentWidget())); d.show(); app.processEvents()
    assert 'town · r1' in d.env_list.toPlainText()
    d.env_pick.setCurrentIndex(d.env_pick.findData('town'))
    d.env_preset.setCurrentIndex(d.env_preset.findData('raised_street'))
    d.env_at[0].setValue(7); d.env_at[1].setValue(18)
    d.stage_environment_place(); app.processEvents()
    assert d.apply_button.isEnabled(), d.status.text()
    d.apply(); app.processEvents()
    assert any(i['header'] == h['tst_town'] for items in p.composed()['terrain_features'].values() for i in items)
    d.load_environment(); app.processEvents()
    assert d.env_action.currentText() == 'revise' and '"raised_street"' in d.env_spec.toPlainText()
    d.close(); w.close(); app.processEvents()
