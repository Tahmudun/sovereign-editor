"""Project transactions for character packages, trainers, persistent state and events."""
import copy
import re
import struct
from . import authoring, character_runtime as cr, dialogue_format as fmt, event_sequences as seq
from . import world, scenery, simple_interactions as simple, event_authoring as ev, scene_authoring as scenes
from .formats import digest, require, resource, file_span, EditorError

SCHEMA = 'sovereign-story-authoring-v1'
KINDS = ('character', 'trainer', 'state', 'sequence')
TRAINER_ARCHIVE = 'a/0/5/5'
PARTY_ARCHIVE = 'a/0/5/6'
TRAINER_OFFSETS = 'a/1/3/1'
TRAINER_MESSAGES = 'a/0/5/7'


def catalog(state, kind): return state.get('story', {}).get(kind, {})

def named(value):
    require(isinstance(value, str) and re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', value),
            'Use a stable name: lowercase letters, numbers, hyphens or underscores', 'INVALID_INPUT')
    return value


def dependencies(project, context):
    return {'baseline': digest(project.blob), 'context': authoring.context_ref(context),
            'event': digest(ev.base(project, context['event_member'])),
            'script': digest(resource(project.blob, fmt.SCRIPT_ARCHIVE, context['header']['script_file'])[1]),
            'text': digest(resource(project.blob, fmt.TEXT_ARCHIVE, context['header']['text_archive'])[1])}


def plan(project, context, state, index, kind, key, value=None, action='put'):
    require(kind in KINDS and action in ('put', 'delete'), 'Unknown story operation', 'INVALID_INPUT')
    named(key); before = catalog(state, kind).get(key)
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
            require(slot < cr.MAX_CHARACTERS, 'Character library supports at most eight entries', 'RESOURCE_CAPACITY')
            require(digest(project.blob) == cr.BASELINE, 'Character runtime requires the pinned baseline', 'UNSUPPORTED_RUNTIME')
        elif kind == 'state':
            require(set(value) == {'name'} and isinstance(value['name'], str) and 1 <= len(value['name']) <= 60,
                    'Persistent state needs a display name', 'INVALID_INPUT')
            require(slot < 16, 'Sixteen persistent state slots are available', 'RESOURCE_CAPACITY')
            after['variable'] = 0x4160 + slot
            qualify_variables(project)
        elif kind == 'trainer':
            require(set(value) == {'name', 'character', 'stock_class', 'party', 'before', 'after'}, 'Invalid trainer fields', 'INVALID_INPUT')
            fmt.encode_message(value['name'])
            require('\n' not in value['name'] and len(value['name']) <= 10, 'Trainer name supports 1..10 characters', 'INVALID_INPUT')
            if value['character']:
                require(value['character'] in catalog(state, 'character') and value['stock_class'] is None,
                        'Choose one imported character', 'INVALID_INPUT')
            else:
                require(type(value['stock_class']) is int and 0 <= value['stock_class'] < 129,
                        'Choose a stock trainer class or imported character', 'INVALID_INPUT')
            require(isinstance(value['party'], list) and 1 <= len(value['party']) <= 6, 'Trainer needs 1..6 Pokémon', 'INVALID_INPUT')
            for mon in value['party']:
                require(isinstance(mon, dict) and set(mon) == {'species', 'level'}, 'Party entries need species and level', 'INVALID_INPUT')
                require(type(mon['species']) is int and 1 <= mon['species'] <= 493 and type(mon['level']) is int and 1 <= mon['level'] <= 100,
                        'Use a base species 1..493 and level 1..100', 'INVALID_INPUT')
            for k in ('before', 'after'):
                require(isinstance(value[k], list) and 1 <= len(value[k]) <= 4, 'Trainer dialogue needs 1..4 pages', 'INVALID_INPUT')
                for page in value[k]: fmt.encode_message(page)
            require(slot < 32, 'Trainer library supports at most 32 definitions', 'RESOURCE_CAPACITY')
            after['trainer_id'] = 738 + slot
        else:
            required = {'kind', 'x', 'z', 'donor_id', 'facing', 'movement', 'range_x', 'range_z', 'character', 'nodes', 'once_state'}
            require(required <= set(value) <= required | scenes.EXTRA_FIELDS,
                    'Invalid event fields', 'INVALID_INPUT')
            require(value['kind'] in ('npc', 'trigger'), 'Choose talk or step-on event', 'INVALID_INPUT')
            require(type(value['facing']) is int and 0 <= value['facing'] <= 3, 'Facing must be 0..3', 'INVALID_INPUT')
            from . import npc_behavior
            npc_behavior.validate_fields(value)
            require(not value['once_state'] or value['once_state'] in catalog(state, 'state'), 'Choose a named one-time state', 'INVALID_INPUT')
            if value['kind'] == 'npc':
                require(value['character'] in catalog(state, 'character') or (value.get('stock_sprite')==552 and value['character'] is None), 'Choose an imported character or Scyther', 'INVALID_INPUT')
            else: require(value['character'] is None and value['movement'] == 0, 'Step-on events have no appearance or movement', 'INVALID_INPUT')
            seq.validate(value['nodes'], catalog(state, 'state'), catalog(state, 'trainer'))
            if before: require(before['context'] == authoring.context_ref(context), 'Choose the event’s original area', 'CONTEXT_MISMATCH')
            from .area_layout import resource_users
            users = resource_users(project, context)
            require(users['events'] == [context['header']['id']], 'Sequence authoring requires private event resources', 'SHARED_RESOURCE')
            donor = next((r for r in ev.records(ev.base(project, context['event_member']))
                          if r['kind'] == 'npc' and r['id'] == value['donor_id']), None)
            require(donor is not None, 'Choose an NPC ground-height donor in this area', 'NOT_FOUND')
            world.cell_offset(context, value['x'], value['z'])
            height=scenes.floor_height if any(k in value for k in scenes.EXTRA_FIELDS) else scenery.floor_height
            require(height(project, context, donor) == height(project, context, value),
                    'The event requires the donor’s verified ground height', 'UNSUPPORTED_HEIGHT')
            after.update(context=authoring.context_ref(context), event_member=context['event_member'],
                         script_member=context['header']['script_file'], text_member=context['header']['text_archive'],
                         y=struct.unpack_from('<i', donor['raw'], 28)[0])
            existing_ids = [r['id'] for r in ev.records(ev.raw_member(project, context['event_member'], state)) if r['kind'] == 'npc']
            after['npc_id'] = before['npc_id'] if before else max(existing_ids, default=-1) + 1
            require(after['npc_id'] < 240, 'No free NPC local ID', 'RESOURCE_CAPACITY')
    deps = dependencies(project, context)
    if kind=='sequence' and after is not None:
        extra=scenes.extend_plan(project,context,state,index,value,after,before)
        if extra:deps.update(extra)
    return {'schema': SCHEMA, 'index': index, 'context': authoring.context_ref(context), 'kind': kind, 'key': key,
            'request': dict(kind=kind, key=key, value=copy.deepcopy(value), action=action),
            'before': copy.deepcopy(before), 'after': after, 'dependencies': deps,
            'dependencies_sha256': authoring.canonical(deps), 'label': f'{action.capitalize()} {kind}: {key}'}


def replay(project, state, t, index):
    try:
        ctx = project.context(header=t['context']['header'], cell=t['context']['cell'])
        expected = plan(project, ctx, state, index, **t['request'])
        require(t == expected, 'Story before-value or dependencies differ', 'BEFORE_VALUE_MISMATCH')
        entries = state.setdefault('story', {}).setdefault(t['kind'], {})
        if t['after'] is None: del entries[t['key']]
        else: entries[t['key']] = t['after']
        if t['kind'] == 'sequence': state['contexts'].append(ctx)
    except (KeyError, TypeError, ValueError, struct.error) as exc:
        raise EditorError('INVALID_INPUT', 'Malformed story transaction') from exc


def qualify_variables(project):
    if getattr(project, '_story_variables_qualified', False): return
    require(digest(project.blob) == cr.BASELINE, 'Persistent state requires the pinned baseline', 'UNSUPPORTED_RUNTIME')
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
    return {k: {'name': p['name'], 'sprite': 7000+i, 'front_class': 129+i, 'back_group': 17+i}
            for i, (k, p) in enumerate(catalog(state, 'character').items())}


def allocation(project, state):
    simple_ids = simple.allocation(project, state); sc = {}; tc = {}; result = {}
    for k, s in simple.specs(state).items():
        script, text = simple_ids[k]; sc[s['script_member']] = script; tc[s['text_member']] = text+1
    for k, s in catalog(state, 'sequence').items():
        sm, tm = s['script_member'], s['text_member']
        if sm not in sc: sc[sm] = len(fmt.script_entries(resource(project.blob, fmt.SCRIPT_ARCHIVE, sm)[1])[1])
        if tm not in tc: tc[tm] = len(fmt.text_entries(resource(project.blob, fmt.TEXT_ARCHIVE, tm)[1])[1])
        result[k] = (sc[sm]+1, tc[tm]); sc[sm] += 1
        tc[tm] += len(seq.messages(s['nodes'], catalog(state, 'trainer')))
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
            if s['kind'] != kind: continue
            if kind == 'npc':
                record=bytearray(simple.record({**s,'sprite':s.get('stock_sprite') or chars[s['character']]['sprite']},ids[key][0]))
                if s.get('hide_flag'):struct.pack_into('<H',record,8,s['hide_flag'])
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
    require(not trainers or chars, 'Trainer authoring requires an imported character runtime', 'INVALID_INPUT')
    for key, s in catalog(state, 'sequence').items():
        seq.validate(s['nodes'], variables, trainers)
        ctx = project.context(header=s['context']['header'], cell=s['context']['cell'])
        offset = world.cell_offset(ctx, s['x'], s['z']); base = project.member_raw(ctx['map_member'])
        pair = state['permissions'].get((ctx['map_member'], offset), base[offset:offset+2])
        # A rectangular trigger may include walls in its bounding box. Gather
        # validates every reachable approach and refuses an empty walkable set.
        require(not world.is_blocked(pair) or s.get('trigger') and any(n['op']=='gather' for n in s['nodes']),
                f'Event {key} stands on blocked terrain', 'BLOCKED_TILE')
        rows = ev.records(ev.raw_member(project, ctx['event_member'], state))
        if s['kind'] == 'npc':
            from . import npc_behavior
            # Stage-exclusive authored actors may share a tile.
            by_id={a['npc_id']:a for a in scenes.actors_for(state,s['event_member']).values()}
            filtered=[r for r in rows if not (r['kind']=='npc' and r['id'] in by_id and scenes.disjoint(s,by_id[r['id']]))]
            if s.get('presence'):filtered=[r for r in filtered if r['kind']!='trigger']
            npc_behavior.validate_area(project, ctx, s, state, filtered,
                                       height_at=scenes.floor_height if s.get('presence') else None)
        try:scenes.validate_routes(project,state,s)
        except EditorError as exc:raise EditorError(exc.code,f'Scene {key}: {exc}') from exc
        for r in rows:
            # Conditional rectangular triggers intentionally surround their actors.
            if s.get('trigger') and r['kind'] in ('npc','trigger'):continue
            if s.get('presence') and r['kind']=='trigger':continue
            if r['kind']=='npc':
                other=next((a for a in scenes.actors_for(state,s['event_member']).values() if a['npc_id']==r['id']),None)
                if other and scenes.disjoint(s,other):continue
            if s['kind'] == 'npc' and r['kind'] == 'npc' and r['id'] == s['npc_id']: continue
            if s['kind'] == 'trigger' and r['kind'] == 'trigger' and r['script'] == allocation(project, state)[key][0]: continue
            if r['kind'] == 'npc': conflict = abs(r['x']-s['x']) <= max(0,r['range_x']) and abs(r['z']-s['z']) <= max(0,r['range_z'])
            elif r['kind'] == 'warp': conflict = r['x'] == s['x'] and r['z'] <= s['z'] <= r['z']+1
            elif r['kind'] == 'trigger': conflict = r['x'] <= s['x'] < r['x']+r['width'] and r['z'] <= s['z'] < r['z']+r['height']
            else: conflict = False
            require(not conflict, 'Event overlaps an actor, entrance or trigger', 'EVENT_CONFLICT')
    replacements(project, state)


def replacements(project, state):
    result = simple.replacements(project, state); texts = {}; scripts = {}
    ids = allocation(project, state); trainers = catalog(state, 'trainer'); variables = catalog(state, 'state')
    for key, s in catalog(state, 'sequence').items():
        texts.setdefault(s['text_member'], []).extend(seq.messages(s['nodes'], trainers))
        scripts.setdefault(s['script_member'], []).append(seq.compile_sequence(s, variables, trainers, ids[key][1],
            {'actors':scenes.actors_for(state,s['event_member']),'variables':variables,
             'routes':scenes.gather_routes(project,state,s)}))
    for archive, groups, append in ((fmt.TEXT_ARCHIVE, texts, fmt.append_messages), (fmt.SCRIPT_ARCHIVE, scripts, fmt.append_scripts)):
        for m, values in groups.items():
            old = result[archive].get(m, resource(project.blob, archive, m)[1]); result[archive][m] = append(old, values)
    scenes.add_initializers(project,state,result)
    chars = character_rows(state)
    if chars:
        for member, label in ((730, 'Trainer'), (731, 'a Trainer')):
            raw = resource(project.blob, fmt.TEXT_ARCHIVE, member)[1]
            require(len(fmt.text_entries(raw)[1]) == 129, 'Trainer class name count differs', 'BEFORE_VALUE_MISMATCH')
            result[fmt.TEXT_ARCHIVE][member] = fmt.append_messages(raw, [label]*len(chars), limit=65535)
    if trainers:
        from . import battle_safety
        result[battle_safety.EFFECT_ARCHIVE] = {
            battle_safety.ATTACK_DOWN: battle_safety.attack_down_effect(
                resource(project.blob, battle_safety.EFFECT_ARCHIVE, battle_safety.ATTACK_DOWN)[1])}
        raw = resource(project.blob, fmt.TEXT_ARCHIVE, 729)[1]
        require(len(fmt.text_entries(raw)[1]) == 738, 'Trainer name count differs', 'BEFORE_VALUE_MISMATCH')
        result[fmt.TEXT_ARCHIVE][729] = fmt.append_messages(raw, [t['name'] for t in trainers.values()], limit=65535)
        offsets = resource(project.blob, TRAINER_OFFSETS, 0)[1]
        table = resource(project.blob, TRAINER_MESSAGES, 0)[1]
        require(len(offsets) == 735*2 and len(table) % 4 == 0 and len(table) < 65536, 'Trainer message lookup differs', 'BEFORE_VALUE_MISMATCH')
        # Empty optional in-battle messages: each new trainer starts at the valid
        # end sentinel. Pre/post dialogue belongs to editable event steps.
        result[TRAINER_OFFSETS] = {0: offsets + struct.pack('<'+'H'*(3+len(trainers)), *([len(table)]*(3+len(trainers))))}
    return result


def runtime(project, state):
    plan = cr.bindings(project.blob, list(catalog(state, 'character').values()), trainer_count=len(catalog(state, 'trainer')))
    classes = character_rows(state)
    if catalog(state, 'trainer'):
        data, parties = [], []
        for t in catalog(state, 'trainer').values():
            c = classes[t['character']]['front_class'] if t['character'] else t['stock_class']
            data.append(struct.pack('<BHB4HIB3x', 0, c, len(t['party']), 0, 0, 0, 0, 0, 0))
            parties.append(b''.join(struct.pack('<BB3H', 0, 0, m['level'], m['species'], 0) for m in t['party']))
        plan['appends'][TRAINER_ARCHIVE] = data; plan['appends'][PARTY_ARCHIVE] = parties
    return plan


def summary(t):
    def compact(v):
        if v is None: return None
        if t['kind'] == 'character': return {'name':v['name'], 'gender':v['gender'], 'package_sha256':authoring.canonical(v)}
        if t['kind'] == 'sequence': return {'kind':v['kind'], 'x':v['x'], 'z':v['z'], 'steps':len(v['nodes']), 'character':v['character']}
        return v
    return {'operation':'story.transaction', 'index':t['index'], 'kind':t['kind'], 'key':t['key'],
            'label':t['label'], 'context':t['context'], 'before':compact(t['before']), 'after':compact(t['after'])}
