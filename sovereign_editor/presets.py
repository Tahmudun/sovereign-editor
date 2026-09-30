"""Reusable event presets (FIELD-05, WORKSPACE-03): each expands into ordinary story operations.

A preset is a compact request that produces the same operations an author could write
by hand (an optional new on/off state, then objects and their events, in dependency
order). The recorded transactions are ordinary story transactions, so history replays
without this module; UI and CLI share this expansion through Project.plan_area_edit.

* ``field_obstacle`` Cut tree / Rock Smash rock / Strength boulder: knows-the-move check,
  optional authority (badge, named state, item or money) with its own refusal page,
  prompt, native field action. ``reset`` returns on the next map load; ``permanent``
  keeps a named on/off state.
* ``gate`` a still object plus a switch event that opens it (permanent shortcut, or
  ``reset``: closes again on re-entry), optionally behind a requirement.
* ``pickup`` an object that grants an item and disappears only after the grant succeeds
  (a full Bag leaves it in place); ``permanent`` never returns, ``reset`` returns on re-entry.
  The find is announced like a stock item ball: pocket fanfare, the item's name and quantity
  and its Bag pocket (``found_item``), unless the author overrides the ``found`` page.
* ``reward`` a one-time giver (item, money or badge) guarded by a named state.
* ``puzzle_reset`` a helper that warps the player to a tile of the same area, which
  reloads the map and returns every Strength boulder to its start.
* ``guide`` a talk-to-travel helper (scripted warp with a qualified arrival).
* ``rematch`` ONE trainer NPC for the whole arc (R82-REMATCH-01): the first battle with the
  original team (a talk-battle trainer whose defeat state records the first win), an optional
  one-time reward (badge or item, guarded by its own on/off state, so it is never given
  twice), then, once the optional condition holds, an offered rematch with the stronger
  team, whose defeat state (or none: repeatable) is the revisit policy. A loss blacks out
  and the same NPC offers the same battle again.
"""
import copy

from .formats import require
from . import field_moves as fm

KINDS = ('field_obstacle', 'gate', 'pickup', 'reward', 'puzzle_reset', 'guide', 'rematch')
MOVE_TEXT = {'cut': ('This tree looks like it\ncan be cut down! Cut it?', 'Cut'),
             'rock_smash': ('This rock looks breakable.\nUse Rock Smash?', 'Rock Smash'),
             'strength': ("It's a big boulder.\nUse Strength?", 'Strength')}


def _npc(x, z, facing, donor, nodes, appearance=None, field=None):
    value = {'kind': 'npc', 'x': x, 'z': z, 'donor_id': donor, 'facing': facing, 'movement': 0, 'range_x': 0,
             'range_z': 0, 'character': None, 'once_state': None, 'nodes': nodes}
    if field is not None:
        value['field'] = field
    else:
        require(isinstance(appearance, int), 'Choose a stock appearance for this helper', 'INVALID_INPUT')
        value['stock_sprite'] = appearance
    return value


def _sequence(context, key, value):
    return {'kind': 'story', 'context': context,
            'request': {'kind': 'sequence', 'key': key, 'action': 'put', 'value': value}}


def _state(context, spec, fallback):
    """(operations, state key): an existing on/off state name, or {'new': key, 'name': text}."""
    if isinstance(spec, dict):
        require(set(spec) == {'new', 'name'}, 'A new state is {new: key, name: text}', 'INVALID_INPUT')
        return [{'kind': 'story', 'context': context, 'request': {
            'kind': 'state', 'key': spec['new'], 'action': 'put', 'value': {'name': spec['name'], 'switch': True}}}], spec['new']
    require(isinstance(spec, str) or spec is None and fallback, 'Choose an on/off state', 'INVALID_INPUT')
    return [], spec


def _blocked(project, authority, move):
    kind = authority['kind']
    if kind == 'badge':
        return f"You need the {fm.BADGES[authority['badge']]} Badge\nto use {move} here."
    if kind == 'item':
        from . import gameplay
        return f"You need {gameplay.name(project, 222, authority['item'])[:14]}\nto use {move} here."
    if kind == 'money':
        return f"It costs {authority['amount']} to\nuse {move} here."
    return f"You need permission\nto use {move} here."


def _authority(authority):
    require(authority is None or isinstance(authority, dict) and authority.get('kind') in fm.REQUIREMENTS
            and authority['kind'] != 'move', 'Authority is a badge, item, state or money requirement', 'INVALID_INPUT')
    return authority


def expand(project, context, request):
    """Story operations for one preset request (validated again when planned)."""
    require(isinstance(request, dict) and request.get('preset') in KINDS, f"Preset is one of {', '.join(KINDS)}",
            'INVALID_INPUT')
    r = copy.deepcopy(request)
    kind, key = r.pop('preset'), r.get('key')
    require(isinstance(key, str) and key, 'A preset needs a stable key', 'INVALID_INPUT')
    texts = r.get('texts') or {}
    require(isinstance(texts, dict), 'texts is an object of page overrides', 'INVALID_INPUT')
    x, z, facing, donor = r.get('x'), r.get('z'), r.get('facing', 1), r.get('donor_id')

    if kind == 'field_obstacle':
        family = r.get('family')
        require(family in fm.FAMILIES, 'Family is cut, rock_smash or strength', 'INVALID_INPUT')
        ask, move = MOVE_TEXT[family]
        persistence = r.get('persistence', 'reset')
        ops, state = _state(context, r.get('state'), persistence == 'reset') if persistence == 'permanent' else ([], None)
        authority = _authority(r.get('authority'))
        nodes = [{'id': 'knows', 'op': 'require', 'kind': 'move', 'move': fm.FAMILIES[family]['move'],
                  'yes': 'auth' if authority else 'ask', 'no': 'nomon'}]
        if authority:
            nodes += [{'id': 'auth', 'op': 'require', **authority, 'yes': 'ask', 'no': 'blocked'},
                      {'id': 'blocked', 'op': 'say', 'pages': [texts.get('blocked') or _blocked(project, authority, move)],
                       'next': 'done'}]
        nodes += [{'id': 'ask', 'op': 'choice', 'pages': [texts.get('ask', ask)], 'yes': 'act', 'no': 'done'},
                  {'id': 'act', 'op': 'field_move', 'move': family, 'yes': 'done', 'no': 'nomon'},
                  {'id': 'nomon', 'op': 'say', 'pages': [texts.get('nomon', f'No Pokémon in your party\nknows {move}.')],
                   'next': 'done'},
                  {'id': 'done', 'op': 'end', 'complete': False}]
        field = {'family': family, 'persistence': persistence, **({'state': state} if state else {})}
        return ops + [_sequence(context, key, _npc(x, z, facing, donor, nodes, field=field))]

    if kind == 'gate':
        persistence = r.get('persistence', 'permanent')
        ops, state = _state(context, r.get('state'), False) if persistence == 'permanent' else ([], None)
        switch = r.get('switch')
        require(isinstance(switch, dict) and isinstance(switch.get('key'), str) and switch['key'] != key,
                'A gate needs a switch {key, x, z, facing, donor_id, appearance}', 'INVALID_INPUT')
        gate_nodes = [{'id': 'shut', 'op': 'say', 'pages': [texts.get('shut', 'The way is blocked.')], 'next': 'done'},
                      {'id': 'done', 'op': 'end', 'complete': False}]
        field = {'family': 'object', 'look': r.get('look', 'rock'), 'persistence': persistence,
                 **({'state': state} if state else {})}
        authority = _authority(switch.get('authority'))
        opened = texts.get('opened', 'The way is open now.')
        nodes = []
        if persistence == 'permanent':
            nodes.append({'id': 'check', 'op': 'if', 'state': state, 'value': 1, 'yes': 'already', 'no': 'auth' if authority else 'ask'})
            nodes.append({'id': 'already', 'op': 'say', 'pages': [texts.get('already', 'The way is already open.')],
                          'next': 'done'})
        if authority:
            nodes += [{'id': 'auth', 'op': 'require', **authority, 'yes': 'ask', 'no': 'blocked'},
                      {'id': 'blocked', 'op': 'say', 'pages': [texts.get('blocked', "It won't budge yet.")], 'next': 'done'}]
        permanent = persistence == 'permanent'
        nodes.append({'id': 'ask', 'op': 'choice', 'pages': [texts.get('ask', 'Open the way?')],
                      'yes': 'mark' if permanent else 'open', 'no': 'done'})
        if permanent:
            nodes.append({'id': 'mark', 'op': 'set', 'state': state, 'value': 1, 'next': 'open'})
        nodes += [{'id': 'open', 'op': 'remove', 'target': key, 'next': 'said'},
                  {'id': 'said', 'op': 'say', 'pages': [opened], 'next': 'done'},
                  {'id': 'done', 'op': 'end', 'complete': False}]
        sx, sz = switch.get('x'), switch.get('z')
        switch_value = _npc(sx, sz, switch.get('facing', 1), switch.get('donor_id'), nodes,
                            appearance=switch.get('appearance') if not isinstance(switch.get('appearance'), str) else None,
                            field=({'family': 'object', 'look': switch['appearance'], 'persistence': 'reset'}
                                   if isinstance(switch.get('appearance'), str) else None))
        return ops + [_sequence(context, key, _npc(x, z, facing, donor, gate_nodes, field=field)),
                      _sequence(context, switch['key'], switch_value)]

    if kind == 'pickup':
        persistence = r.get('persistence', 'permanent')
        ops, state = _state(context, r.get('state'), False) if persistence == 'permanent' else ([], None)
        require(type(r.get('item')) is int and type(r.get('count', 1)) is int, 'A pickup needs an item and count',
                'INVALID_INPUT')
        count = r.get('count', 1)
        found = ({'id': 'found', 'op': 'say', 'pages': [texts['found']], 'next': 'done'} if 'found' in texts else
                 {'id': 'found', 'op': 'found_item', 'item': r['item'], 'count': count, 'next': 'done'})
        nodes = [{'id': 'give', 'op': 'give_item', 'item': r['item'], 'count': count, 'yes': 'take', 'no': 'full'},
                 {'id': 'take', 'op': 'remove', 'target': 'self', 'next': 'found'},
                 found,
                 {'id': 'full', 'op': 'say', 'pages': [texts.get('full', 'Your Bag is full.\nMake room and come back.')],
                  'next': 'done'},
                 {'id': 'done', 'op': 'end', 'complete': False}]
        field = {'family': 'object', 'look': r.get('look', 'ball'), 'persistence': persistence,
                 **({'state': state} if state else {})}
        return ops + [_sequence(context, key, _npc(x, z, facing, donor, nodes, field=field))]

    if kind == 'reward':
        ops, state = _state(context, r.get('state'), False)
        give = r.get('give')
        require(isinstance(give, dict) and len(give) >= 1, 'A reward gives {item, count}, {money} or {badge}',
                'INVALID_INPUT')
        if 'item' in give:
            grant = {'id': 'give', 'op': 'give_item', 'item': give['item'], 'count': give.get('count', 1), 'yes': 'mark',
                     'no': 'full'}
        elif 'money' in give:
            grant = {'id': 'give', 'op': 'give_money', 'amount': give['money'], 'next': 'mark'}
        else:
            require('badge' in give, 'A reward gives {item, count}, {money} or {badge}', 'INVALID_INPUT')
            grant = {'id': 'give', 'op': 'give_badge', 'badge': give['badge'], 'next': 'mark'}
        nodes = [{'id': 'check', 'op': 'if', 'state': state, 'value': 1, 'yes': 'after', 'no': 'offer'},
                 {'id': 'offer', 'op': 'say', 'pages': texts.get('offer', ['This is for you.']), 'next': 'give'},
                 grant,
                 {'id': 'mark', 'op': 'set', 'state': state, 'value': 1, 'next': 'thanks'},
                 {'id': 'thanks', 'op': 'say', 'pages': texts.get('thanks', ['Use it well!']), 'next': 'done'},
                 {'id': 'after', 'op': 'say', 'pages': texts.get('after', ['I have nothing more for you.']),
                  'next': 'done'},
                 {'id': 'done', 'op': 'end', 'complete': False}]
        if grant['op'] == 'give_item':
            nodes.insert(-1, {'id': 'full', 'op': 'say', 'pages': [texts.get('full', 'Your Bag is full.\nMake room and come back.')],
                              'next': 'done'})
        return ops + [_sequence(context, key, _npc(x, z, facing, donor, nodes, appearance=r.get('appearance')))]

    if kind == 'rematch':
        original, again = r.get('original'), r.get('trainer')
        require(isinstance(original, str) and isinstance(again, str) and original != again,
                'A rematch names the original trainer and a different rematch trainer', 'INVALID_INPUT')
        library = (project.composed() if project is not None else {}).get('story', {}).get('trainer', {})
        if original in library:
            require(library[original].get('defeat_state'), 'The first team needs a defeat state so this NPC '
                    'remembers the first win', 'INVALID_INPUT')
        condition = _authority(r.get('condition'))
        reward = r.get('reward')
        require(reward is None or isinstance(reward, dict) and (set(reward) == {'badge'} or set(reward) <= {'item', 'count'}
                and 'item' in reward), 'A reward is {badge} or {item, count}', 'INVALID_INPUT')
        ops, state = _state(context, r.get('reward_state'), False) if reward else ([], None)
        nodes = [{'id': 'beaten', 'op': 'require', 'kind': 'trainer', 'trainer': original,
                  'yes': 'rewarded' if reward else 'ready' if condition else 'offer', 'no': 'fight'},
                 {'id': 'fight', 'op': 'battle', 'trainer': original, 'won': 'award' if reward else 'done'}]
        if reward:
            if 'badge' in reward:
                grant = {'id': 'give', 'op': 'give_badge', 'badge': reward['badge'], 'next': 'mark'}
                said = texts.get('reward', [f"Take the {fm.BADGES[reward['badge']]} Badge!"])
            else:
                grant = {'id': 'give', 'op': 'give_item', 'item': reward['item'], 'count': reward.get('count', 1),
                         'yes': 'mark', 'no': 'full'}
                said = texts.get('reward', ['Take this as proof\nof your win!'])
            # The first win and the reward are separate states: a reward that could not be
            # given (full Bag) is offered again on the next visit, never twice.
            nodes += [{'id': 'award', 'op': 'if', 'state': state, 'value': 1, 'yes': 'done', 'no': 'give'},
                      {'id': 'rewarded', 'op': 'if', 'state': state, 'value': 1,
                       'yes': 'ready' if condition else 'offer', 'no': 'give'},
                      grant,
                      {'id': 'mark', 'op': 'set', 'state': state, 'value': 1, 'next': 'said'},
                      {'id': 'said', 'op': 'say', 'pages': said, 'next': 'done'}]
            if 'item' in reward:
                nodes.append({'id': 'full', 'op': 'say', 'pages': [texts.get('full', 'Your Bag is full.\nMake room and come back.')],
                              'next': 'done'})
        if condition:
            nodes += [{'id': 'ready', 'op': 'require', **condition, 'yes': 'offer', 'no': 'later'},
                      {'id': 'later', 'op': 'say', 'pages': texts.get('later', ['Train hard. We will\nbattle again someday.']),
                       'next': 'done'}]
        nodes += [{'id': 'offer', 'op': 'choice', 'pages': texts.get('offer', ['How about a rematch?\nI brought my best team!']),
                   'yes': 'rematch', 'no': 'declined'},
                  {'id': 'rematch', 'op': 'battle', 'trainer': again, 'won': 'done'},
                  {'id': 'declined', 'op': 'say', 'pages': texts.get('declined', ['Come back any time.']), 'next': 'done'},
                  {'id': 'done', 'op': 'end', 'complete': False}]
        return ops + [_sequence(context, key, _npc(x, z, facing, donor, nodes, appearance=r.get('appearance')))]

    if kind in ('puzzle_reset', 'guide'):
        dest = r.get('destination')
        require(isinstance(dest, dict) and set(dest) <= {'header', 'x', 'z', 'facing'},
                'Choose a destination {header, x, z, facing}', 'INVALID_INPUT')
        if kind == 'puzzle_reset':
            dest = {'header': context['header'], **dest}
            ask = texts.get('ask', 'Put every boulder back\nwhere it started?')
        else:
            ask = texts.get('ask', 'Shall I take you there?')
        nodes = [{'id': 'ask', 'op': 'choice', 'pages': [ask], 'yes': 'go', 'no': 'later'},
                 {'id': 'go', 'op': 'warp', **dest},
                 {'id': 'later', 'op': 'say', 'pages': [texts.get('later', 'Come back any time.')], 'next': 'done'},
                 {'id': 'done', 'op': 'end', 'complete': False}]
        return [_sequence(context, key, _npc(x, z, facing, donor, nodes, appearance=r.get('appearance')))]
    raise AssertionError(kind)
