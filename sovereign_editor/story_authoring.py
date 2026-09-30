"""Project transactions for character packages, trainers, persistent state and events."""
import copy
import re
import struct
from . import authoring, character_runtime as cr, dialogue_format as fmt, event_sequences as seq, storage
from . import world, scenery, simple_interactions as simple, event_authoring as ev, scene_authoring as scenes
from .formats import baseline_digest, digest, require, resource, file_span, EditorError

SCHEMA = 'sovereign-story-authoring-v1'
# v2: Scyther is stock sprite 597, entry scenes, follower-aware scene routes.
# v1 transactions replay under v1 rules, so r28 history keeps its meaning.
SCENE_SCHEMA = 'sovereign-story-authoring-v2'
# v3 (PROD-CAP-001): library capacity 32 characters / 64 trainers / 158 states, and
# on/off switch states in qualified flags. Scenes keep v2 rules; v1/v2 library
# transactions replay under their original limits and allocations.
CAPACITY_SCHEMA = 'sovereign-story-authoring-v3'
SCHEMAS = (SCHEMA, SCENE_SCHEMA, CAPACITY_SCHEMA)
LIMITS = {1: {'character': 8, 'trainer': 32}, 2: {'character': 8, 'trainer': 32},
          3: {'character': 32, 'trainer': 64}}
KINDS = ('character', 'trainer', 'state', 'sequence')
TRAINER_ARCHIVE = 'a/0/5/5'
PARTY_ARCHIVE = 'a/0/5/6'
TRAINER_OFFSETS = 'a/1/3/1'
TRAINER_MESSAGES = 'a/0/5/7'


# Explicit, versioned battle policy. Trainers without one are historical practice
# trainers: pre/post healing, local win/loss branches and BATTLE_TYPE_11 return.
ORDINARY = 'ordinary-single-v1'
ORDINARY_FIELDS = {'policy', 'defeat_state', 'revisit'}
ORDINARY_AI = 1   # F_PRIORITIZE_SUPER_EFFECTIVE, as stock early-route trainers (Mikey47)
# ordinary-v2 (trainer_format.py): optional fields beyond name/appearance/party/texts.
V2_FIELDS = {'defeat_state', 'battle', 'ai', 'items', 'defeat', 'insufficient'}


def catalog(state, kind): return state.get('story', {}).get(kind, {})


def ordinary_trainer(project, state, key, value):
    """Stock single battle: chosen moves/items, blackout on loss, named defeat state."""
    from . import gameplay, trainer_format as tf
    require(value['policy'] in (ORDINARY, tf.POLICY), 'Unknown trainer battle policy', 'INVALID_INPUT')
    v2 = value['policy'] == tf.POLICY
    states = catalog(state, 'state')
    # v2: without a defeat state the talk battle repeats (native placements use the trainer flag).
    if not v2 or value.get('defeat_state') is not None:
        require(value.get('defeat_state') in states, 'Ordinary trainers need a named defeat state', 'INVALID_INPUT')
        for other, t in catalog(state, 'trainer').items():
            require(other == key or t.get('defeat_state') != value['defeat_state'],
                    'Each ordinary trainer needs its own defeat state', 'STATE_CONFLICT')
    modes = set()
    from . import species_forms as sf
    for mon in value['party']:
        if v2:
            require(isinstance(mon, dict) and {'species', 'level', 'moves', 'held_item'} <= set(mon) <= tf.MON_FIELDS,
                    'Party entries need species, level, moves and held_item', 'INVALID_INPUT')
        else:
            require(isinstance(mon, dict) and set(mon) in ({'species', 'level', 'moves', 'held_item'},
                                                           {'species', 'form', 'level', 'moves', 'held_item'}),
                    'Ordinary party entries need species, level, moves and held_item (optional form)', 'INVALID_INPUT')
        if 'form' in mon or type(mon['species']) is int and mon['species'] > 493:
            sf.require_supported(project, mon['species'], mon.get('form', 0), 'Trainer Pokémon')
        else:
            gameplay.valid_id(project, 'species', mon['species'])
        if v2 and mon['held_item']:
            # v2 offers qualified expanded items/moves too (game_data catalogs, DATA-03).
            from . import game_data
            e = game_data.item_entry(project, state, mon['held_item']) if type(mon['held_item']) is int else None
            require(e is not None and e['holdable'], f"Held item {mon['held_item']}: "
                    f"{(e or {}).get('hold_reason') or (e or {}).get('reason') or 'unsupported'}", 'UNSUPPORTED_ID')
        else:
            gameplay.valid_id(project, 'items', mon['held_item'])
        gameplay.integer(mon['level'], 1, 100, 'Level')
        moves = mon['moves']; modes.add(moves is None)
        if moves is not None:
            require(isinstance(moves, list) and len(moves) == 4 and any(moves), 'Choose four move slots, at least one nonempty', 'INVALID_INPUT')
            for move in moves:
                if v2 and move:
                    from . import game_data
                    e = game_data.move_entry(project, state, move) if type(move) is int else None
                    require(e is not None and e['supported'], f"Move {move}: {(e or {}).get('reason') or 'unsupported'}",
                            'UNSUPPORTED_ID')
                else:
                    gameplay.valid_id(project, 'moves', move)
            require(len([m for m in moves if m]) == len({m for m in moves if m}), 'Duplicate moves are unsupported', 'INVALID_INPUT')
    require(len(modes) == 1, 'Default/custom moves is shared by the whole trainer team', 'INVALID_INPUT')
    if v2:
        tf.validate(project, value)


def encode_ordinary(t, trainer_class):
    """Header and party bytes in the qualified custom layout (types 0..3)."""
    moves = t['party'][0]['moves'] is not None; items = any(m['held_item'] for m in t['party'])
    kind = (1 if moves else 0) | (2 if items else 0)
    header = struct.pack('<BHB4HIB3x', kind, trainer_class, len(t['party']), 0, 0, 0, 0, ORDINARY_AI, 0)
    rows = b''
    for m in t['party']:
        rows += struct.pack('<BBHH', 0, 0, m['level'], m['species'] | m.get('form', 0) << 11)
        rows += struct.pack('<H', m['held_item']) if items else b''
        rows += struct.pack('<4H', *m['moves']) if moves else b''
        rows += struct.pack('<H', 0)
    return header, rows

def named(value):
    require(isinstance(value, str) and re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', value),
            'Use a stable name: lowercase letters, numbers, hyphens or underscores', 'INVALID_INPUT')
    return value


def dependencies(project, context):
    return {'baseline': baseline_digest(project), 'context': authoring.context_ref(context),
            'event': digest(ev.base(project, context['event_member'])),
            'script': digest(project.resource(fmt.SCRIPT_ARCHIVE, context['header']['script_file'])[1]),
            'text': digest(project.resource(fmt.TEXT_ARCHIVE, context['header']['text_archive'])[1])}


# Fields through which one definition names another (PROD-02 reference checks).
REF_FIELDS = {'character': ('character',), 'trainer': ('trainer', 'partner', 'opponent2'),
              'state': ('state', 'once_state', 'defeat_state')}


def _named_refs(value, kind):
    found = set()
    def walk(v):
        if isinstance(v, dict):
            for k, item in v.items():
                if k in REF_FIELDS[kind] and isinstance(item, str):
                    found.add(item)
                walk(item)
        elif isinstance(v, list):
            for item in v:
                walk(item)
    walk(value)
    return found


def references(state, kind, key):
    """Active definitions that name ``key`` of ``kind`` (retired definitions are inert)."""
    users = []
    for other_kind in KINDS:
        for other, value in catalog(state, other_kind).items():
            if (other_kind, other) == (kind, key) or value.get('retired'):
                continue
            if key in _named_refs(value, kind):
                users.append(f'{other_kind} {other}')
    return sorted(users)


def plan(project, context, state, index, kind, key, value=None, action='put', _version=None):
    require(kind in KINDS and action in ('put', 'delete', 'retire'), 'Unknown story operation', 'INVALID_INPUT')
    if _version is None:
        _version = 2 if kind == 'sequence' else 3
    named(key); before = catalog(state, kind).get(key)
    if action == 'retire':
        # PROD-02: library IDs (character classes, trainer IDs, state variables/flags) may be in saves,
        # so a definition is retired, never deleted or reused; new definitions get fresh IDs.
        require(kind in ('character', 'trainer', 'state') and value is None,
                'Retire takes a character, trainer or state key', 'INVALID_INPUT')
        require(before is not None, f'No {kind} {key}', 'NOT_FOUND')
        require(not before.get('retired'), f'{kind.capitalize()} {key} is already retired', 'NO_CHANGE')
        users = references(state, kind, key)
        require(not users, f"Still used by {', '.join(users)}", 'IN_USE')
        after = {**copy.deepcopy(before), 'retired': True}
        deps = dependencies(project, context)
        return {'schema': SCHEMAS[_version - 1], 'index': index, 'context': authoring.context_ref(context), 'kind': kind,
                'key': key, 'request': dict(kind=kind, key=key, value=None, action=action),
                'before': copy.deepcopy(before), 'after': after, 'dependencies': deps,
                'dependencies_sha256': authoring.canonical(deps), 'label': f'Retire {kind}: {key}'}
    require(before is None or not before.get('retired'), f'{kind.capitalize()} {key} is retired; its ID stays reserved',
            'RETIRED')
    if action == 'put' and isinstance(value, dict):
        for ref_kind in ('character', 'trainer', 'state'):
            gone = sorted(k for k in _named_refs(value, ref_kind) if catalog(state, ref_kind).get(k, {}).get('retired'))
            require(not gone, f"Retired {ref_kind} cannot be used: {', '.join(gone)}", 'RETIRED')
    if action == 'delete':
        require(kind == 'sequence' and before is not None and value is None,
                'Only authored sequences can be deleted; library IDs remain stable', 'INVALID_INPUT')
        require(before['context'] == authoring.context_ref(context), 'Choose the event’s original area', 'CONTEXT_MISMATCH')
        after = None
    else:
        require(isinstance(value, dict), 'Definition must be an object', 'INVALID_INPUT')
        after = copy.deepcopy(value)
        slot = list(catalog(state, kind)).index(key) if before else len(catalog(state, kind))
        if kind == 'character':
            after = cr.validate_package(value)
            limit = LIMITS[_version]['character']
            require(slot < limit, 'Character library supports at most eight entries' if limit == 8 else
                    f'Character library supports at most {limit} entries', 'RESOURCE_CAPACITY')
            require(baseline_digest(project) == cr.BASELINE, 'Character runtime requires the pinned baseline', 'UNSUPPORTED_RUNTIME')
        elif kind == 'state' and _version >= 3:
            after = storage.allocate_state(project, catalog(state, 'state'), key, value, before)
            qualify_variables(project)
        elif kind == 'state':
            require(set(value) == {'name'} and isinstance(value['name'], str) and 1 <= len(value['name']) <= 60,
                    'Persistent state needs a display name', 'INVALID_INPUT')
            require(slot < 16, 'Sixteen persistent state slots are available', 'RESOURCE_CAPACITY')
            after['variable'] = 0x4160 + slot
            qualify_variables(project)
        elif kind == 'trainer':
            fields = {'name', 'character', 'stock_class', 'party', 'before', 'after'}
            ordinary = 'policy' in value
            from . import trainer_format
            if ordinary and value['policy'] == trainer_format.POLICY:
                require(fields | {'policy', 'revisit'} <= set(value) <= fields | {'policy', 'revisit'} | V2_FIELDS,
                        'Invalid trainer fields', 'INVALID_INPUT')
            else:
                require(set(value) == (fields | ORDINARY_FIELDS if ordinary else fields), 'Invalid trainer fields', 'INVALID_INPUT')
            fmt.encode_message(value['name'])
            require('\n' not in value['name'] and len(value['name']) <= 10, 'Trainer name supports 1..10 characters', 'INVALID_INPUT')
            if value['character']:
                require(value['character'] in catalog(state, 'character') and value['stock_class'] is None,
                        'Choose one imported character', 'INVALID_INPUT')
            else:
                require(type(value['stock_class']) is int and 0 <= value['stock_class'] < 129,
                        'Choose a stock trainer class or imported character', 'INVALID_INPUT')
            require(isinstance(value['party'], list) and 1 <= len(value['party']) <= 6, 'Trainer needs 1..6 Pokémon', 'INVALID_INPUT')
            if ordinary:
                ordinary_trainer(project, state, key, value)
            else:
                for mon in value['party']:
                    require(isinstance(mon, dict) and set(mon) == {'species', 'level'}, 'Party entries need species and level', 'INVALID_INPUT')
                    require(type(mon['species']) is int and 1 <= mon['species'] <= 493 and type(mon['level']) is int and 1 <= mon['level'] <= 100,
                            'Use a base species 1..493 and level 1..100', 'INVALID_INPUT')
            for k in ('before', 'after') + (('revisit',) if ordinary else ()) + tuple(f for f in ('defeat', 'insufficient') if f in value):
                require(isinstance(value[k], list) and 1 <= len(value[k]) <= 4, 'Trainer dialogue needs 1..4 pages', 'INVALID_INPUT')
                for page in value[k]: fmt.encode_message(page)
            limit = LIMITS[_version]['trainer']
            require(slot < limit, f'Trainer library supports at most {limit} definitions', 'RESOURCE_CAPACITY')
            after['trainer_id'] = 738 + slot
        else:
            required = {'kind', 'x', 'z', 'donor_id', 'facing', 'movement', 'range_x', 'range_z', 'character', 'nodes', 'once_state'}
            from . import native_trainers
            native = native_trainers.is_native(value)
            require(required <= set(value) <= required | scenes.EXTRA_FIELDS | {'field'} | (native_trainers.FIELDS if native else set()),
                    'Invalid event fields', 'INVALID_INPUT')
            require(value['kind'] in ('npc', 'trigger') or _version >= 2 and value['kind'] == 'entry' or native,
                    'Choose talk, step-on, entry or trainer event', 'INVALID_INPUT')
            require(type(value['facing']) is int and 0 <= value['facing'] <= 3, 'Facing must be 0..3', 'INVALID_INPUT')
            from . import npc_behavior
            npc_behavior.validate_fields(value)
            require(not value['once_state'] or value['once_state'] in catalog(state, 'state'), 'Choose a named one-time state', 'INVALID_INPUT')
            # A step-on record tests a variable directly; flags cannot gate it.
            require(value['kind'] != 'trigger' or not value['once_state']
                    or not storage.is_switch(catalog(state, 'state')[value['once_state']]),
                    'Step-on events need a number state as their one-time state', 'INVALID_EVENT')
            if native:
                native_trainers.plan(project, context, state, value, catalog(state, 'trainer'))
            elif value['kind'] == 'npc':
                require(value['character'] in catalog(state, 'character') or (value.get('stock_sprite') is not None and value['character'] is None)
                        or value.get('field') and value['character'] is None, 'Choose an imported character or a stock appearance', 'INVALID_INPUT')
            else: require(value['character'] is None and value['movement'] == 0, 'Step-on and entry events have no appearance or movement', 'INVALID_INPUT')
            if not native:
                seq.validate(value['nodes'], catalog(state, 'state'), catalog(state, 'trainer'))
                qualify_nodes(project, value['nodes'], state)
            field_deps = None
            if value.get('field') is not None:
                from . import field_moves
                field_deps = field_moves.plan_field(project, context, state, key, value, after, before)
            else:
                require(not any(n['op'] == 'field_move' for n in value['nodes']),
                        'Field move steps belong to a field obstacle event', 'INVALID_EVENT')
            if before: require(before['context'] == authoring.context_ref(context), 'Choose the event’s original area', 'CONTEXT_MISMATCH')
            from .area_layout import resource_users
            users = resource_users(project, context)
            require(users['events'] == [context['header']['id']], 'Sequence authoring requires private event resources', 'SHARED_RESOURCE')
            from . import world_authoring
            height=scenes.floor_height if any(k in value for k in scenes.EXTRA_FIELDS) else scenery.floor_height
            if value['donor_id'] is None and world_authoring.created(project, context):
                # Created areas have no stock actors to borrow from. Stock NPC records
                # use height word 0 on flat ground (the engine follows BDHC); require
                # one verified flat floor at the tile instead of a donor comparison.
                world.cell_offset(context, value['x'], value['z'])
                height(project, context, value); y = 0
            else:
                donor = next((r for r in ev.records(ev.base(project, context['event_member']))
                              if r['kind'] == 'npc' and r['id'] == value['donor_id']), None)
                require(donor is not None, 'Choose an NPC ground-height donor in this area', 'NOT_FOUND')
                world.cell_offset(context, value['x'], value['z'])
                require(height(project, context, donor) == height(project, context, value),
                        'The event requires the donor’s verified ground height', 'UNSUPPORTED_HEIGHT')
                y = struct.unpack_from('<i', donor['raw'], 28)[0]
            after.update(context=authoring.context_ref(context), event_member=context['event_member'],
                         script_member=context['header']['script_file'], text_member=context['header']['text_archive'],
                         y=y)
            existing_ids = [r['id'] for r in ev.records(ev.raw_member(project, context['event_member'], state)) if r['kind'] == 'npc']
            after['npc_id'] = before['npc_id'] if before else max(existing_ids, default=-1) + 1
            if any(n['op'] == 'remove' for n in value['nodes']):
                from .field_moves import check_targets
                check_targets(state, key, after)
            require(after['npc_id'] < 240, 'No free NPC local ID', 'RESOURCE_CAPACITY')
    deps = dependencies(project, context)
    if kind=='sequence' and after is not None:
        extra=scenes.extend_plan(project,context,state,index,value,after,before,_version)
        if extra:deps.update(extra)
        if field_deps:deps.update(field_deps)
    return {'schema': SCHEMAS[_version-1], 'index': index, 'context': authoring.context_ref(context), 'kind': kind, 'key': key,
            'request': dict(kind=kind, key=key, value=copy.deepcopy(value), action=action),
            'before': copy.deepcopy(before), 'after': after, 'dependencies': deps,
            'dependencies_sha256': authoring.canonical(deps), 'label': f'{action.capitalize()} {kind}: {key}'}


def qualify_nodes(project, nodes, state=None):
    """Project-level checks of gifts, service steps (roster, moves, cost items) and
    travel destinations (clear arrival and follower tiles in the composed world)."""
    from . import services, species_forms as sf, travel, field_moves, field_services
    for n in nodes:
        if n.get('op') == 'give_mon' and (n.get('form') or n['species'] > 493):
            sf.require_supported(project, n['species'], n.get('form', 0), 'Gift')
        elif n.get('op') in services.OPS:
            services.qualify(project, n)
        elif n.get('op') in travel.OPS and state is not None:
            travel.qualify(project, state, n)
        elif n.get('op') in field_moves.OPS:
            field_moves.qualify(project, n)
        elif n.get('op') in field_services.OPS and state is not None:
            field_services.qualify(project, state, n)


def replay(project, state, t, index):
    try:
        ctx = project.context(header=t['context']['header'], cell=t['context']['cell'])
        version = SCHEMAS.index(t['schema']) + 1
        expected = plan(project, ctx, state, index, **t['request'], _version=version)
        require(t == expected, 'Story before-value or dependencies differ', 'BEFORE_VALUE_MISMATCH')
        entries = state.setdefault('story', {}).setdefault(t['kind'], {})
        if t['after'] is None: del entries[t['key']]
        else: entries[t['key']] = t['after']
        if t['kind'] == 'sequence':
            state['contexts'].append(ctx)
            # Scene qualification rules follow the writer, not the current code.
            versions = state.setdefault('scene_versions', {})
            if t['after'] is None: versions.pop(t['key'], None)
            else: versions[t['key']] = version
    except (KeyError, TypeError, ValueError, struct.error) as exc:
        raise EditorError('INVALID_INPUT', 'Malformed story transaction') from exc


def changed(state, t):
    """A put changes the record, or brings an older scene under the current rules once.

    Re-applying an unchanged quest batch must not leave r28-era scenes qualified only
    by v1 rules (no follower occupancy); a second identical put is a no-op.
    """
    if t['before'] != t['after']: return True
    return (t['kind'] == 'sequence' and t['after'] is not None
            and state.get('scene_versions', {}).get(t['key'], 1) < SCHEMAS.index(t['schema']) + 1)


def qualify_variables(project):
    if getattr(project, '_story_variables_qualified', False): return
    require(baseline_digest(project) == cr.BASELINE, 'Persistent state requires the pinned baseline', 'UNSUPPORTED_RUNTIME')
    # The range is inside NUM_VARS, unnamed in the pinned engine, unused by all
    # script members and coordinate-event conditions. Source/binary audit is in
    # evidence/tiana-events-1; these runtime checks guard the authored inputs.
    _, raw = file_span(project.blob, fmt.SCRIPT_ARCHIVE)
    from .formats import member_span
    count = struct.unpack_from('<H', raw, 24)[0]
    for i in range(count):
        member = member_span(raw, i)[1]
        require(all(struct.pack('<H', v) not in member for v in range(0x4160, 0x4170)),
                'Reserved state range occurs in baseline scripts', 'STATE_CONFLICT')
    _, raw = file_span(project.blob, world.EVENT_ARCHIVE)
    for i in range(struct.unpack_from('<H', raw, 24)[0]):
        member = member_span(raw, i)[1]; cursor = 0
        # Scan coordinate records directly: some unused stock members have
        # duplicate NPC IDs, which are irrelevant to this state-range check.
        for size in (20, 32, 12, 16):
            count = struct.unpack_from('<I', member, cursor)[0]; cursor += 4
            require(cursor + count*size <= len(member), 'Invalid baseline event section')
            if size == 16:
                for slot in range(count):
                    require(struct.unpack_from('<H', member, cursor + slot*size + 14)[0] not in range(0x4160, 0x4170),
                            'Reserved state range occurs in a coordinate event', 'STATE_CONFLICT')
            cursor += count*size
        require(cursor == len(member), 'Unexpected baseline event bytes')
    project._story_variables_qualified = True


def character_rows(state):
    return {k: {'name': p['name'], 'sprite': 7000+i, 'front_class': 129+i, 'back_group': 17+i,
                **({'retired': True} if p.get('retired') else {})}
            for i, (k, p) in enumerate(catalog(state, 'character').items())}


def allocation(project, state):
    simple_ids = simple.allocation(project, state); sc = {}; tc = {}; result = {}
    for k, s in simple.specs(state).items():
        script, text = simple_ids[k]; sc[s['script_member']] = script; tc[s['text_member']] = text+1
    for k, s in catalog(state, 'sequence').items():
        if s['kind'] == 'trainer': continue   # native trainer: stock std script, no local script
        sm, tm = s['script_member'], s['text_member']
        if sm not in sc: sc[sm] = len(fmt.script_entries(project.resource(fmt.SCRIPT_ARCHIVE, sm)[1])[1])
        if tm not in tc: tc[tm] = len(fmt.text_entries(project.resource(fmt.TEXT_ARCHIVE, tm)[1])[1])
        result[k] = (sc[sm]+1, tc[tm]); sc[sm] += 1
        tc[tm] += len(seq.messages(s['nodes'], catalog(state, 'trainer')))
        require(sc[sm] < 1000 and tc[tm] <= 256, 'Area script/text capacity exceeded', 'RESOURCE_CAPACITY')
    # Respawn-point arrival scripts (travel_points) follow every sequence of their map.
    from . import travel_points
    for key, sm, tm, pages in travel_points.arrival_scripts(project, state):
        if sm not in sc: sc[sm] = len(fmt.script_entries(project.resource(fmt.SCRIPT_ARCHIVE, sm)[1])[1])
        if tm not in tc: tc[tm] = len(fmt.text_entries(project.resource(fmt.TEXT_ARCHIVE, tm)[1])[1])
        result['travel:' + key] = (sc[sm]+1, tc[tm]); sc[sm] += 1; tc[tm] += len(pages)
        require(sc[sm] < 1000 and tc[tm] <= 256, 'Area script/text capacity exceeded', 'RESOURCE_CAPACITY')
    return result


def append_events(project, member, state, raw):
    selected = [(k, s) for k, s in catalog(state, 'sequence').items() if s['event_member'] == member]
    if not selected: return raw
    ids = allocation(project, state); chars = character_rows(state)
    cursor = 0; result = bytearray()
    for kind, size in [('background',20), ('npc',32), ('warp',12), ('trigger',16)]:
        count = struct.unpack_from('<I', raw, cursor)[0]; cursor += 4; added = []
        for key, s in selected:
            if s['kind'] == 'trainer' and kind == 'npc':
                from . import native_trainers
                t = catalog(state, 'trainer')[s['trainer']]
                look = chars[t['character']]['sprite'] if t['character'] else s['stock_sprite']
                added.append(native_trainers.record(s, t, look)); continue
            if s['kind'] != kind: continue
            if kind == 'npc':
                if s.get('field'):
                    from .field_moves import sprite
                    look = sprite(s)
                else:
                    look = s.get('stock_sprite') or chars[s['character']]['sprite']
                record=bytearray(simple.record({**s,'sprite':look},ids[key][0]))
                if s.get('hide_flag'):struct.pack_into('<H',record,8,s['hide_flag'])
                if s.get('field_flag'):struct.pack_into('<H',record,8,s['field_flag'])
                added.append(bytes(record))
            else:
                variable = catalog(state, 'state')[s['once_state']]['variable'] if s['once_state'] else 0
                require(s['y'] == 0, 'Step-on events currently require ground height zero', 'UNSUPPORTED_HEIGHT')
                t=s.get('trigger') or {}
                if t:variable=catalog(state,'state')[t['state']]['variable']
                added.append(struct.pack('<8H',ids[key][0],s['x'],s['z'],t.get('width',1),t.get('height',1),0,t.get('value',0),variable))
        result.extend(struct.pack('<I', count+len(added))); result.extend(raw[cursor:cursor+count*size]); result.extend(b''.join(added)); cursor += count*size
    require(cursor == len(raw) and len(result) <= 0x800, 'Area event buffer exceeded', 'RESOURCE_CAPACITY')
    return bytes(result)


def validate(project, state):
    if not state.get('story'): return
    scenes.validate_triggers(project,state)
    chars = catalog(state, 'character'); trainers = catalog(state, 'trainer'); variables = catalog(state, 'state')
    # Stock-class trainers need no imported character (character_runtime builds trainer hooks alone).
    from . import native_trainers
    placements = [s for s in catalog(state, 'sequence').values() if native_trainers.is_native(s)]
    for key, s in catalog(state, 'sequence').items():
        if not native_trainers.is_native(s):
            seq.validate(s['nodes'], variables, trainers)
        if any(n['op'] == 'remove' for n in s['nodes']):
            from .field_moves import check_targets
            check_targets(state, key, s)
        # A later edit must not block a travel destination or remove a shop this event relies on.
        try:
            qualify_nodes(project, [n for n in s['nodes'] if n['op'] in ('warp', 'shop', 'set_spawn')], state)
        except EditorError as exc:
            raise EditorError(exc.code, f'Event {key}: {exc}') from exc
        ctx = project.context(header=s['context']['header'], cell=s['context']['cell'])
        offset = world.cell_offset(ctx, s['x'], s['z']); base = project.member_raw(ctx['map_member'])
        pair = state['permissions'].get((ctx['map_member'], offset), base[offset:offset+2])
        # A rectangular trigger may include walls in its bounding box. Gather
        # validates every reachable approach and refuses an empty walkable set.
        require(not world.is_blocked(pair) or s.get('trigger') and any(n['op']=='gather' for n in s['nodes']),
                f'Event {key} stands on blocked terrain', 'BLOCKED_TILE')
        rows = ev.records(ev.raw_member(project, ctx['event_member'], state))
        if native_trainers.is_native(s):
            native_trainers.validate(project, state, key, s, rows, trainers, placements)
        if s['kind'] == 'npc':
            from . import npc_behavior
            # Stage-exclusive authored actors may share a tile.
            by_id={a['npc_id']:a for a in scenes.actors_for(state,s['event_member']).values()}
            filtered=[r for r in rows if not (r['kind']=='npc' and r['id'] in by_id and scenes.disjoint(s,by_id[r['id']]))]
            if s.get('presence'):filtered=[r for r in filtered if r['kind']!='trigger']
            npc_behavior.validate_area(project, ctx, s, state, filtered,
                                       height_at=scenes.floor_height if s.get('presence') else None)
        if s['kind'] == 'entry':
            require(any(r['kind'] == 'warp' and (r['x'], r['z']) == (s['x'], s['z']) for r in rows),
                    f'Entry event {key} must be placed on an entrance the player arrives on', 'INVALID_EVENT')
        try:scenes.validate_routes(project,state,s,state.get('scene_versions',{}).get(key,1))
        except EditorError as exc:raise EditorError(exc.code,f'Scene {key}: {exc}') from exc
        for r in rows:
            # Conditional rectangular triggers intentionally surround their actors.
            if s.get('trigger') and r['kind'] in ('npc','trigger'):continue
            if s.get('presence') and r['kind']=='trigger':continue
            if r['kind']=='npc':
                other=next((a for a in scenes.actors_for(state,s['event_member']).values() if a['npc_id']==r['id']),None)
                if other and scenes.disjoint(s,other):continue
            if s['kind'] in ('npc', 'trainer') and r['kind'] == 'npc' and r['id'] == s['npc_id']: continue
            if s['kind'] == 'trigger' and r['kind'] == 'trigger' and r['script'] == allocation(project, state)[key][0]: continue
            if r['kind'] == 'npc': conflict = abs(r['x']-s['x']) <= max(0,r['range_x']) and abs(r['z']-s['z']) <= max(0,r['range_z'])
            elif r['kind'] == 'warp': conflict = s['kind'] != 'entry' and r['x'] == s['x'] and r['z'] <= s['z'] <= r['z']+1
            elif r['kind'] == 'trigger': conflict = r['x'] <= s['x'] < r['x']+r['width'] and r['z'] <= s['z'] < r['z']+r['height']
            else: conflict = False
            require(not conflict, 'Event overlaps an actor, entrance or trigger', 'EVENT_CONFLICT')
    replacements(project, state)


def replacements(project, state):
    from . import travel_points, tutor_labels
    tutor_index = tutor_labels.index(state)
    result = simple.replacements(project, state); texts = {}; scripts = {}
    ids = allocation(project, state); trainers = catalog(state, 'trainer'); variables = catalog(state, 'state')
    from .field_services import shop_index
    shop_ids = shop_index(state)
    for key, s in catalog(state, 'sequence').items():
        if s['kind'] == 'trainer': continue
        texts.setdefault(s['text_member'], []).extend(seq.messages(s['nodes'], trainers))
        scripts.setdefault(s['script_member'], []).append((seq.compile_sequence(s, variables, trainers, ids[key][1],
            {'actors':scenes.actors_for(state,s['event_member']),'variables':variables,
             'routes':scenes.gather_routes(project,state,s),'shops':shop_ids,
             'travel':{k: p['spawn'] for k, p in travel_points.points(state).items()},
             'tutors':{i: pos for (k, i), pos in tutor_index.items() if k == key}}), seq.alignment(s)))
    for key, sm, tm, pages in travel_points.arrival_scripts(project, state):
        texts.setdefault(tm, []).extend(pages)
        scripts.setdefault(sm, []).append((travel_points.compile_arrival(ids['travel:' + key][1], len(pages)), 1))
    for m, values in texts.items():
        old = result[fmt.TEXT_ARCHIVE].get(m, project.resource(fmt.TEXT_ARCHIVE, m)[1])
        result[fmt.TEXT_ARCHIVE][m] = fmt.append_messages(old, values)
    for m, values in scripts.items():
        old = result[fmt.SCRIPT_ARCHIVE].get(m, project.resource(fmt.SCRIPT_ARCHIVE, m)[1])
        result[fmt.SCRIPT_ARCHIVE][m] = fmt.append_scripts(old, [code for code, _ in values], [a for _, a in values])
    scenes.add_initializers(project,state,result)
    chars = character_rows(state)
    if chars:
        for member, label in ((730, 'Trainer'), (731, 'a Trainer')):
            raw = resource(project.blob, fmt.TEXT_ARCHIVE, member)[1]
            require(len(fmt.text_entries(raw)[1]) == 129, 'Trainer class name count differs', 'BEFORE_VALUE_MISMATCH')
            result[fmt.TEXT_ARCHIVE][member] = fmt.append_messages(raw, [label]*len(chars), limit=65535)
    if trainers:
        # The r27 Attack-down bypass is superseded by runtime()'s overlay repairs,
        # which restore the native limit message for every stat.
        raw = resource(project.blob, fmt.TEXT_ARCHIVE, 729)[1]
        require(len(fmt.text_entries(raw)[1]) == 738, 'Trainer name count differs', 'BEFORE_VALUE_MISMATCH')
        result[fmt.TEXT_ARCHIVE][729] = fmt.append_messages(raw, [t['name'] for t in trainers.values()], limit=65535)
        offsets = resource(project.blob, TRAINER_OFFSETS, 0)[1]
        table = resource(project.blob, TRAINER_MESSAGES, 0)[1]
        require(len(offsets) == 735*2 and len(table) % 4 == 0 and len(table) < 65536, 'Trainer message lookup differs', 'BEFORE_VALUE_MISMATCH')
        # Empty optional in-battle messages: each new trainer starts at the valid
        # end sentinel. Pre/post dialogue belongs to editable event steps.
        result[TRAINER_OFFSETS] = {0: offsets + struct.pack('<'+'H'*(3+len(trainers)), *([len(table)]*(3+len(trainers))))}
    from . import native_trainers
    native = native_trainers.tables(project, state, trainers,
                                    [s for s in catalog(state, 'sequence').values() if native_trainers.is_native(s)])
    for name, members in native.items():
        target = result.setdefault(name, {})
        if name != TRAINER_OFFSETS:
            require(not set(target) & set(members), f'Native trainer tables overlap other edits in {name}', 'RESOURCE_CONFLICT')
        target.update(members)
    return result


def runtime(project, state, layout=None):
    trainers = list(catalog(state, 'trainer').values())
    practice = [not t.get('policy') for t in trainers]
    plan = cr.bindings(project.blob, list(catalog(state, 'character').values()), trainer_count=len(trainers),
                       practice=None if all(practice) else practice, layout=layout)
    classes = character_rows(state)
    if catalog(state, 'trainer'):
        data, parties = [], []
        for t in catalog(state, 'trainer').values():
            c = classes[t['character']]['front_class'] if t['character'] else t['stock_class']
            if t.get('policy'):
                from . import trainer_format
                header, party = (trainer_format.encode(t, c) if t['policy'] == trainer_format.POLICY
                                 else encode_ordinary(t, c))
                data.append(header); parties.append(party)
                continue
            data.append(struct.pack('<BHB4HIB3x', 0, c, len(t['party']), 0, 0, 0, 0, 0, 0))
            parties.append(b''.join(struct.pack('<BB3H', 0, 0, m['level'], m['species'], 0) for m in t['party']))
        plan['appends'][TRAINER_ARCHIVE] = data; plan['appends'][PARTY_ARCHIVE] = parties
        # Battles need the capped-stat loop (002) and lower-clamp (003) repairs.
        from . import battle_safety
        for ovy, repair in ((137, battle_safety.stat_stage_overlay), (142, battle_safety.before_move_overlay)):
            info = cr.overlay(project.blob, ovy)
            plan['files'][info['file_id']] = repair(info['data'])
    if any(n['op']=='collect' for s in catalog(state,'sequence').values() for n in s['nodes']):
        from . import scene_runtime
        plan=scene_runtime.bindings(project.blob,plan,layout=layout)
    return plan


def summary(t):
    def compact(v):
        if v is None: return None
        if t['kind'] == 'character': return {'name':v['name'], 'gender':v['gender'], 'package_sha256':authoring.canonical(v)}
        if t['kind'] == 'sequence': return {'kind':v['kind'], 'x':v['x'], 'z':v['z'], 'steps':len(v['nodes']), 'character':v['character']}
        return v
    return {'operation':'story.transaction', 'index':t['index'], 'kind':t['kind'], 'key':t['key'],
            'label':t['label'], 'context':t['context'], 'before':compact(t['before']), 'after':compact(t['after'])}
