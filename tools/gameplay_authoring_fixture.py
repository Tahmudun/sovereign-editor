"""Gameplay authoring v1 technical fixture as ONE atomic area batch.

Values are demonstrations for native checks, not approved game balance. Every
before-value is read from the target Project at build time; nothing is written
here. Apply with Project.apply_area_edit (or `sovereign area-edit`).
"""
import copy
import json
import sys
from pathlib import Path

ROUTE29 = {'header': 33, 'cell': [18, 12]}
STATES = {'robin_defeated': 'Robin defeated', 'kit_sentret': 'Test kit: Sentret given',
          'kit_pidgey': 'Test kit: Pidgey given', 'kit_candy': 'Test kit: Rare Candy given',
          'kit_tm': 'Test kit: TM017 given'}
SENTRET, FURRET, PIDGEY, RATTATA = 161, 162, 16, 19
RARE_CANDY, TM017, TM017_INDEX = 50, 344, 16
GIRL, SCIENTIST = 320, 338

ROBIN = {'name': 'Robin', 'character': None, 'stock_class': 3, 'policy': 'ordinary-single-v1',
         'defeat_state': 'robin_defeated',
         'party': [{'species': PIDGEY, 'level': 4, 'moves': [16, 33, 28, 0], 'held_item': 0},
                   {'species': RATTATA, 'level': 5, 'moves': [98, 33, 39, 0], 'held_item': 155}],
         'before': ['Ready for a battle?\nHere I come!'], 'after': ['Good battle!'],
         'revisit': ['You already won this one.']}


def robin_event():
    nodes = [
        {'id': 'check', 'op': 'if', 'state': 'robin_defeated', 'value': 1, 'yes': 'fight', 'no': 'ask'},
        {'id': 'ask', 'op': 'choice', 'pages': ['I train on Route 29.\nWant a real battle?'], 'yes': 'fight', 'no': 'decline'},
        {'id': 'fight', 'op': 'battle', 'trainer': 'robin', 'won': 'done'},
        {'id': 'decline', 'op': 'say', 'pages': ['Come back when ready.'], 'next': 'done'},
        {'id': 'done', 'op': 'end', 'complete': False}]
    return {'kind': 'npc', 'x': 581, 'z': 394, 'donor_id': 2, 'facing': 1, 'movement': 0, 'range_x': 0,
            'range_z': 0, 'character': None, 'stock_sprite': GIRL, 'nodes': nodes, 'once_state': None}


def kit_event():
    def grant(key, state, give, success, next_key, failure):
        return [
            {'id': key, 'op': 'if', 'state': state, 'value': 1, 'yes': next_key, 'no': key + '_give'},
            {**give, 'id': key + '_give', 'yes': key + '_set', 'no': failure},
            {'id': key + '_set', 'op': 'set', 'state': state, 'value': 1, 'next': key + '_sound'},
            {'id': key + '_sound', 'op': 'sound', 'sound': 'receive', 'next': key + '_say'},
            {'id': key + '_say', 'op': 'say', 'pages': [success], 'next': next_key}]
    nodes = [{'id': 'hello', 'op': 'say', 'pages': ['Progression test kit:\nPokémon, candy and a TM.'], 'next': 'sentret'}]
    nodes += grant('sentret', 'kit_sentret', {'op': 'give_mon', 'species': SENTRET, 'level': 4},
                   'You got a Lv. 4 Sentret!', 'pidgey', 'party_full')
    nodes += grant('pidgey', 'kit_pidgey', {'op': 'give_mon', 'species': PIDGEY, 'level': 4},
                   'You got a Lv. 4 Pidgey!', 'candy', 'party_full')
    nodes += grant('candy', 'kit_candy', {'op': 'give_item', 'item': RARE_CANDY, 'count': 4},
                   'You got 4 Rare Candies!', 'tm', 'bag_full')
    nodes += grant('tm', 'kit_tm', {'op': 'give_item', 'item': TM017, 'count': 2},
                   'You got 2 TM017 (Protect)!', 'done', 'bag_full')
    nodes += [
        {'id': 'party_full', 'op': 'say', 'pages': ['Your party is full.\nMake room and come back.'], 'next': 'end'},
        {'id': 'bag_full', 'op': 'say', 'pages': ['Your Bag has no room.\nMake room and come back.'], 'next': 'end'},
        {'id': 'done', 'op': 'say', 'pages': ['That is the whole kit.\nGood luck with the checks!'], 'next': 'end'},
        {'id': 'end', 'op': 'end', 'complete': False}]
    return {'kind': 'npc', 'x': 583, 'z': 394, 'donor_id': 2, 'facing': 1, 'movement': 0, 'range_x': 0,
            'range_z': 0, 'character': None, 'stock_sprite': SCIENTIST, 'nodes': nodes, 'once_state': None}


def species_operations(project):
    sentret, furret, pidgey = (project.gameplay_species(s) for s in (SENTRET, FURRET, PIDGEY))
    level = next(e for e in sentret['evolution'] if e['method'] == 4 and e['target'] == FURRET)
    return [
        {'kind': 'species', 'id': SENTRET, 'before_sha256': sentret['before_sha256'], 'changes': {
            'stats': {'hp': 40, 'defense': 38, 'speed': 25}, 'types': [0, 4], 'abilities': [51, 50], 'growth': 4,
            'learnset': {'add': [{'level': 5, 'move': 98}]},
            'evolutions': [{'slot': level['slot'], 'level': 6, 'target': FURRET}],
            'machines': [{'machine': TM017_INDEX, 'compatible': True}]}},
        {'kind': 'species', 'id': FURRET, 'before_sha256': furret['before_sha256'], 'changes': {
            'types': [0, 4], 'growth': 4, 'machines': [{'machine': TM017_INDEX, 'compatible': True}]}},
        {'kind': 'species', 'id': PIDGEY, 'before_sha256': pidgey['before_sha256'], 'changes': {
            'stats': {'speed': 60}, 'machines': [{'machine': TM017_INDEX, 'compatible': False}]}}]


def encounter_operations(project):
    r29, r30 = project.gameplay_data(33)['encounters'], project.gameplay_data(34)['encounters']
    grass29 = r29['methods']['grass']; edits29 = []
    for slot, level in enumerate([3, 4, 3, 4, 3, 4, 3, 4, 4, 4, 4, 4]):
        if grass29['levels'][slot] != level:
            edits29.append({'method': 'grass', 'field': 'level', 'slot': slot, 'value': level})
    for slot, species in enumerate(grass29['day']):
        if grass29['night'][slot] != species:
            edits29.append({'method': 'grass', 'field': 'species', 'time': 'night', 'slot': slot, 'value': species})
    grass30 = r30['methods']['grass']; edits30 = []
    if grass30['levels'][1] != 4:
        edits30.append({'method': 'grass', 'field': 'level', 'slot': 1, 'value': 4})
    if grass30['night'][1] != SENTRET:
        edits30.append({'method': 'grass', 'field': 'species', 'time': 'night', 'slot': 1, 'value': SENTRET})
    return [{'kind': 'encounters', 'header': 33, 'before_sha256': r29['before_sha256'], 'edits': edits29},
            {'kind': 'encounters', 'header': 34, 'before_sha256': r30['before_sha256'], 'edits': edits30}]


def batch(project):
    library = project.story_library()
    ops = []
    for key, name in STATES.items():
        if key not in library['states']:
            ops.append({'kind': 'story', 'context': ROUTE29, 'request': {'kind': 'state', 'key': key, 'value': {'name': name}}})
    ops.append({'kind': 'story', 'context': ROUTE29, 'request': {'kind': 'trainer', 'key': 'robin', 'value': copy.deepcopy(ROBIN)}})
    ops.append({'kind': 'story', 'context': ROUTE29, 'request': {'kind': 'sequence', 'key': 'robin', 'value': robin_event()}})
    ops.append({'kind': 'story', 'context': ROUTE29, 'request': {'kind': 'sequence', 'key': 'test_kit', 'value': kit_event()}})
    ops.append({'kind': 'gameplay', 'context': ROUTE29, 'request': {
        'operations': species_operations(project) + encounter_operations(project)}})
    return {'operations': ops, 'label': 'Gameplay authoring v1 fixture'}


if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from sovereign_editor.core import Project
    print(json.dumps(batch(Project(sys.argv[1])), indent=1))
