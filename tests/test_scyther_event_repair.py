"""Scene support on the delivered r28 project, read-only: plans and previews, no writes.

Checks the actual Scyther binding, that r28's v1 history keeps replaying while new
edits use v2 rules, and a sample eastern-house entry scene compiled into the real
header-71 files. The final quest composition is separate (Astra); this is software
evidence, not melonDS acceptance.
"""
import copy
from pathlib import Path

import pytest

from sovereign_editor.core import Project
from sovereign_editor.formats import EditorError, resource
from sovereign_editor import dialogue_format as fmt, scene_authoring as sa, story_authoring as story
from tests.test_scene_entry_follower import movement_targets, native_commands, scene_script_id

ROOT = Path(__file__).resolve().parents[1]
R28 = ROOT / 'projects/scyther-quest-1'
pytestmark = pytest.mark.skipif(not (R28 / 'project.json').exists(), reason='r28 project absent')
QUEST = 'scyther_quest'


@pytest.fixture(scope='module')
def r28():
    before = (R28 / 'project.json').read_bytes()
    yield Project(R28)
    assert (R28 / 'project.json').read_bytes() == before


def put(header, key, value, cell=(0, 0)):
    return {'kind': 'story', 'context': {'header': header, 'cell': list(cell)},
            'request': {'kind': 'sequence', 'key': key, 'value': value}}


def request_value(project, key):
    value = copy.deepcopy(story.catalog(project.composed(), 'sequence')[key])
    for k in ('context', 'event_member', 'script_member', 'text_member', 'y', 'npc_id', 'hide_flag'):
        value.pop(k, None)
    return value


def test_actual_scyther_binding(r28):
    sa.qualify_stock_sprite(r28, 597, 2)
    assert sa.sprite_row(r28, 597)[1] == 421 and sa.sprite_row(r28, 552)[1] == 384
    with pytest.raises(EditorError, match=r'Scyther \(597\)'):
        sa.qualify_stock_sprite(r28, 552, 2)
    sa.qualify_stock_sprite(r28, 552, 1)                  # v1 history keeps its recorded tag
    with pytest.raises(EditorError):
        sa.qualify_stock_sprite(r28, 597, 1)
    sprites = r28.story_library()['stock_sprites']
    # Scyther stays the only Pokémon scene sprite; qualified human appearances follow.
    assert sprites[0] == {'stock_sprite': 597, 'name': 'Scyther'} and all(s['stock_sprite'] < 364 for s in sprites[1:])


def test_r28_history_replays_and_new_edits_use_scyther_597(r28):
    assert r28.doc['revision'] == 28
    assert {t['schema'] for t in r28.doc['map_edits'] if 'story' in t.get('schema', '')} == {story.SCHEMA}
    assert set(r28.composed()['scene_versions'].values()) == {1}
    beach = request_value(r28, 'scyther_beach')
    assert beach['stock_sprite'] == 552
    with pytest.raises(EditorError, match=r'Scyther \(597\)'):
        r28.plan_area_edit([put(67, 'scyther_beach', beach, (16, 12))])
    plan = r28.plan_area_edit([put(67, 'scyther_beach', {**beach, 'stock_sprite': 597}, (16, 12))])
    (t,) = plan['transactions']
    assert t['schema'] == story.SCENE_SCHEMA and t['before']['stock_sprite'] == 552 and t['after']['stock_sprite'] == 597


def house_operations():
    stay = {'state': QUEST, 'values': [0, 8]}
    tiana = dict(kind='npc', x=2, z=6, donor_id=0, facing=3, movement=0, range_x=0, range_z=0, character='tiana',
                 once_state=None, presence=stay, nodes=[dict(id='hello', op='say', pages=['Thanks again!'], next='end'),
                                                        dict(id='end', op='end')])
    entry = dict(kind='entry', x=4, z=8, donor_id=0, facing=0, movement=0, range_x=0, range_z=0, character=None,
                 once_state=None, trigger={'state': QUEST, 'value': 0, 'advance': 1}, nodes=[
                     dict(id='gather', op='gather', destination=[4, 7], next='notice', no='late'),
                     dict(id='notice', op='reaction', actor='tiana_house', reaction='exclamation', next='intro'),
                     dict(id='intro', op='say', pages=['An injured Scyther\nwas seen by the sea.'], next='leave'),
                     dict(id='leave', op='move', actor='tiana_house', path=[[2, 6], [2, 7], [3, 7]], speed='run', next='search'),
                     dict(id='late', op='say', pages=['Tiana hurried out\ntoward the sea.'], next='search'),
                     dict(id='search', op='set', state=QUEST, value=1, next='sync'),
                     dict(id='sync', op='sync', next='end'), dict(id='end', op='end')])
    return [put(71, 'tiana_house', tiana), put(71, 'house_entry', entry)]


def test_eastern_house_entry_scene_compiles_into_the_real_map_files(r28):
    plan = r28.plan_area_edit(house_operations())
    assert [t['schema'] for t in plan['transactions']] == [story.SCENE_SCHEMA] * 2
    preview = r28.area_preview_project(plan); state = preview.composed()
    assert state['scene_versions']['house_entry'] == 2
    result = story.replacements(preview, state)
    ctx = preview.context(header=71, cell=[0, 0]); header = ctx['header']
    init = result[fmt.SCRIPT_ARCHIVE][header['level_script']]
    variable = story.catalog(state, 'state')[QUEST]['variable']
    entry_id = story.allocation(preview, state)['house_entry'][0]
    assert scene_script_id(init, {variable: 0}) == entry_id and scene_script_id(init, {variable: 1}) == 0xFFFF
    assert [t for _, t, _ in sa.init_records(init)] == [sa.FRAME_TABLE, sa.TRANSITION]
    # Every authored movement block, in every changed map script, is 4-aligned and
    # reads back through the emulated LDRH as it was written.
    for member in (header['script_file'], 850, 225, 856):
        raw = result[fmt.SCRIPT_ARCHIVE][member]
        stock = len(fmt.script_entries(resource(r28.blob, fmt.SCRIPT_ARCHIVE, member)[1])[1])
        start = fmt.script_entries(raw)[1][stock]
        targets = movement_targets(raw, start)
        assert targets and all(t % 4 == 0 and native_commands(raw, t) for _, t in targets), member
    # The eastern house script really contains the frame-table entry scene.
    raw = result[fmt.SCRIPT_ARCHIVE][header['script_file']]
    body = raw[fmt.script_entries(raw)[1][entry_id - 1]:]
    assert body.startswith(bytes.fromhex('1100') + variable.to_bytes(2, 'little'))   # stage guard first


def test_follower_rules_refuse_an_npc_route_across_the_trailing_follower(r28):
    # Gathering two tiles in leaves the follower on (4,7); Tiana may not end there.
    ops = house_operations(); nodes = ops[1]['request']['value']['nodes']
    nodes[0]['destination'] = [4, 6]; nodes[3]['path'] = [[2, 6], [2, 7], [4, 7]]
    with pytest.raises(EditorError, match='following Pokémon may occupy at \\(4, 7\\)'):
        r28.plan_area_edit(ops)
    nodes[3]['path'] = [[2, 6], [2, 7], [3, 7]]
    r28.plan_area_edit(ops)
