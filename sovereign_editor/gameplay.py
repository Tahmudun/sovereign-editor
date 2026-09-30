"""Qualified trainer teams, wild encounters and species data. Project owns all writes.

v1 transactions (Route 30 only) replay exactly under their original rules.
v2 adds compatible-area discovery, species/learnset/evolution/machine records and
row-level changes; teams keep their native trainer scripts. Expanded species/forms
and custom trainer field bits stay read-only. Evidence and offsets:
docs/GAMEPLAY_DATA_IMPLEMENTATION.md and docs/GAMEPLAY_AUTHORING_V1_IMPLEMENTATION.md.
"""
import copy
import struct

from . import authoring, dialogue_format as text, event_authoring, world
from .character_runtime import BASELINE
from .formats import EditorError, baseline_digest, digest, file_span, member_span, require

SCHEMA = 'sovereign-gameplay-v1'
SCHEMA_V2 = 'sovereign-gameplay-v2'
SCHEMAS = (SCHEMA, SCHEMA_V2)
SPECIES_KINDS = ('types', 'abilities', 'growth', 'machines')
TRAINERS = 'a/0/5/5'
PARTIES = 'a/0/5/6'
WILD = 'a/0/3/7'
CATALOGS = {'species': ('a/0/0/2', 237, 493, 44),
            'moves': ('a/0/1/1', 750, 467, 16),
            'items': ('a/0/1/7', 222, 536, 36)}
TIMES = ('morning', 'day', 'night')
METHODS = {'surf': (100, 5, 1), 'rock_smash': (120, 2, 2),
           'old_rod': (128, 5, 3), 'good_rod': (148, 5, 4), 'super_rod': (168, 5, 5)}
POLICY = 'Existing native trainer: no added healing; native defeat, blackout and rematch rules.'


def archive(project, path):
    cache = getattr(project, '_gameplay_archives', None)
    if cache is None:
        project._gameplay_archives = cache = {}
    if path not in cache:
        cache[path] = file_span(project.blob, path)[1]
    return cache[path]


def base(project, path, member):
    if project.created_member(path, member):
        return project.resource(path, member)[1]
    return member_span(archive(project, path), member)[1]


def current(project, state, path, member):
    return state.get('gameplay_members', {}).get((path, member), base(project, path, member))


def name(project, bank, index):
    cache = getattr(project, '_gameplay_names', None)
    if cache is None:
        project._gameplay_names = cache = {}
    if bank not in cache:
        cache[bank] = text.text_entries(base(project, text.TEXT_ARCHIVE, bank))[1]
    entries = cache[bank]
    if not 0 <= index < len(entries):
        return None
    words = [c ^ (((index + 1) * 596947 + j * 18749) & 65535)
             for j, (c,) in enumerate(struct.iter_unpack('<H', entries[index][2]))]
    if words and words[0] == 0xf100:
        packed = words[1:words.index(65535)]
        bits = sum((w & 32767) << (15 * i) for i, w in enumerate(packed))
        words = [(bits >> (9 * i)) & 511 for i in range(len(packed) * 15 // 9)]
        if 511 in words:
            words = words[:words.index(511)]
    chars = {v: k for k, v in text.CHARS.items()}
    if any(c not in chars and c != 65535 for c in words):
        return None
    return ''.join(chars[c] for c in words if c != 65535)


def qualify(project):
    require(baseline_digest(project) == BASELINE, 'Gameplay data requires the qualified build', 'UNSUPPORTED_RUNTIME')


def entry(project, kind, index):
    require(kind in CATALOGS and type(index) is int and index >= 0, 'Invalid catalog reference', 'INVALID_INPUT')
    path, bank, maximum, size = CATALOGS[kind]
    raw = archive(project, path)
    count = struct.unpack_from('<H', raw, 24)[0]
    label = name(project, bank, index)
    data = member_span(raw, index)[1] if index < count else b''
    supported = 0 < index <= maximum and bool(label) and len(data) == size
    reason = '' if supported else 'Outside the qualified base catalog'
    if supported and kind == 'moves' and (data[11] & 0x20 or not data[6]):
        supported, reason = False, 'Move is disabled or has no PP in this build'
    if supported and kind == 'items' and not data[2]:
        supported, reason = False, 'This slice supports items with a held effect'
    if index == 0 and kind in ('moves', 'items'):
        supported, reason, label = True, '', 'Empty move' if kind == 'moves' else 'No item'
    return {'id': index, 'name': label or f'Unnamed {kind} {index}', 'supported': supported, 'reason': reason}


EXTRA_KINDS = ('species_forms', 'evolution_methods', 'evolution_items')


def catalog(project, kind, search='', offset=0, limit=40):
    qualify(project)
    if kind in EXTRA_KINDS:
        require(isinstance(search, str) and type(offset) is int and offset >= 0 and type(limit) is int
                and 1 <= limit <= 600, 'Invalid catalog query', 'INVALID_INPUT')
        from . import species as sp, species_forms as sf
        if kind == 'species_forms':
            return sf.catalog(project, search, offset, limit)
        if kind == 'evolution_methods':
            rows = [{'id': n, 'name': k, 'parameter': param, 'supported': True, 'reason': ''}
                    for k, (n, param) in sp.EVOLUTION_METHODS.items()]
        else:
            rows = [{'id': i, 'name': name(project, 222, i) or f'Item {i}', 'supported': True, 'reason': ''}
                    for i in sp.evolution_items(project)]
        rows = [r for r in rows if search.lower() in r['name'].lower() or search == str(r['id'])]
        return {'kind': kind, 'total': len(rows), 'offset': offset, 'entries': rows[offset:offset + limit]}
    require(kind in (*CATALOGS, *SPECIES_KINDS) and isinstance(search, str) and type(offset) is int and offset >= 0
            and type(limit) is int and 1 <= limit <= 100, 'Invalid catalog query', 'INVALID_INPUT')
    if kind in SPECIES_KINDS:
        from . import species
        rows = [r for r in species.catalog_rows(project, kind) if search.lower() in r['name'].lower() or search == str(r['id'])]
        return {'kind': kind, 'scope': 'qualified base values; expanded values are preserved but not offered',
                'total': len(rows), 'offset': offset, 'entries': rows[offset:offset+limit]}
    _, _, maximum, _ = CATALOGS[kind]
    rows = [entry(project, kind, i) for i in range(0 if kind != 'species' else 1, maximum + 1)]
    rows = [r for r in rows if search.lower() in r['name'].lower() or search == str(r['id'])]
    return {'kind': kind, 'scope': 'qualified base IDs; expanded IDs are not enabled',
            'total': len(rows), 'offset': offset, 'entries': rows[offset:offset+limit]}


def valid_id(project, kind, value):
    e = entry(project, kind, value)
    require(e['supported'], f"Unsupported {kind} ID {value}: {e['reason']}", 'UNSUPPORTED_ID')


def integer(value, low, high, what):
    require(type(value) is int and low <= value <= high, f'{what} must be {low}..{high}', 'INVALID_INPUT')


def decode_team(header, party):
    require(len(header) == 20 and header[0] in range(4) and 1 <= header[3] <= 6,
            'Unsupported trainer fields, type or party count', 'UNSUPPORTED_TRAINER')
    stride = 8 + (8 if header[0] & 1 else 0) + (2 if header[0] & 2 else 0)
    require(len(party) == stride * header[3], 'Trainer party size differs', 'UNSUPPORTED_TRAINER')
    rows = []
    for offset in range(0, len(party), stride):
        difficulty, override, level, packed = struct.unpack_from('<BBHH', party, offset)
        at = offset + 6
        item = struct.unpack_from('<H', party, at)[0] if header[0] & 2 else 0
        at += 2 if header[0] & 2 else 0
        moves = list(struct.unpack_from('<4H', party, at)) if header[0] & 1 else None
        rows.append({'species': packed & 2047, 'form': packed >> 11, 'level': level,
                     'moves': moves, 'held_item': item, 'difficulty': difficulty,
                     'ability_override': override, 'capsule': struct.unpack_from('<H', party, offset + stride - 2)[0]})
    return rows


def team_value(rows):
    return [{k: r[k] for k in ('species', 'level', 'moves', 'held_item')} for r in rows]


def encode_team(project, header, party, value):
    old = decode_team(header, party)
    require(all(r['form'] == 0 for r in old), 'Form-bearing parties are read-only in this slice', 'UNSUPPORTED_TRAINER')
    for r in old:
        valid_id(project, 'species', r['species']); valid_id(project, 'items', r['held_item'])
        for move in r['moves'] or []:
            valid_id(project, 'moves', move)
    require(isinstance(value, list) and 1 <= len(value) <= 6, 'Team needs 1..6 Pokémon', 'INVALID_INPUT')
    modes = set()
    for m in value:
        require(isinstance(m, dict) and set(m) == {'species', 'level', 'moves', 'held_item'}, 'Invalid team fields', 'INVALID_INPUT')
        valid_id(project, 'species', m['species']); valid_id(project, 'items', m['held_item'])
        integer(m['level'], 1, 100, 'Level')
        moves = m['moves']; modes.add(moves is None)
        if moves is not None:
            require(isinstance(moves, list) and len(moves) == 4 and any(moves), 'Choose four move slots, at least one nonempty', 'INVALID_INPUT')
            for move in moves:
                valid_id(project, 'moves', move)
            require(len([v for v in moves if v]) == len(set(v for v in moves if v)), 'Duplicate moves are unsupported', 'INVALID_INPUT')
    require(len(modes) == 1, 'Default/custom moves is shared by the whole trainer team', 'INVALID_INPUT')
    h = bytearray(header)
    h[0] = (1 if value[0]['moves'] is not None else 0) | (2 if header[0] & 2 or any(m['held_item'] for m in value) else 0)
    h[3] = len(value)
    result = bytearray()
    for i, m in enumerate(value):
        opaque = old[i] if i < len(old) else {'difficulty': 0, 'ability_override': 0, 'capsule': 0}
        result.extend(struct.pack('<BBHH', opaque['difficulty'], opaque['ability_override'], m['level'], m['species']))
        if h[0] & 2:
            result.extend(struct.pack('<H', m['held_item']))
        if h[0] & 1:
            result.extend(struct.pack('<4H', *m['moves']))
        result.extend(struct.pack('<H', opaque['capsule']))
    return bytes(h), bytes(result)


def decode_wild(raw):
    require(len(raw) == 196, 'Unsupported encounter record size', 'UNSUPPORTED_ENCOUNTERS')
    result = {'grass': {'rate': raw[0], 'levels': list(raw[8:20]),
                       **{t: list(struct.unpack_from('<12H', raw, 20 + i*24)) for i, t in enumerate(TIMES)}}}
    for name_, (offset, count, rate) in METHODS.items():
        result[name_] = {'rate': raw[rate], 'slots': [dict(zip(('min_level', 'max_level', 'species'),
                           struct.unpack_from('<BBH', raw, offset+i*4))) for i in range(count)]}
    return result


def encode_wild(project, raw, edits):
    decode_wild(raw)
    require(isinstance(edits, list) and 1 <= len(edits) <= 100, 'Use 1..100 encounter edits', 'INVALID_INPUT')
    result = bytearray(raw); touched = set()
    def put(offset, data):
        cells = set(range(offset, offset + len(data)))
        require(not cells & touched, 'An encounter field is edited twice', 'DUPLICATE_EDIT')
        touched.update(cells); result[offset:offset+len(data)] = data
    for e in edits:
        require(isinstance(e, dict) and e.get('method') in ('grass', *METHODS), 'Unknown encounter method', 'INVALID_INPUT')
        method, field = e['method'], e.get('field')
        common = {'method', 'field', 'value'}
        if field == 'rate':
            require(set(e) == common, 'Invalid rate fields', 'INVALID_INPUT')
            integer(e['value'], 0, 100, 'Stored encounter rate')
            put(0 if method == 'grass' else METHODS[method][2], bytes([e['value']]))
        elif method == 'grass':
            integer(e.get('slot'), 0, 11, 'Grass slot')
            if field == 'level':
                require(set(e) == common | {'slot'}, 'Grass levels are shared across all three times', 'INVALID_INPUT')
                integer(e['value'], 1, 100, 'Level'); put(8+e['slot'], bytes([e['value']]))
            else:
                require(field == 'species' and set(e) == common | {'slot', 'time'} and e['time'] in TIMES,
                        'Choose grass species and morning/day/night', 'INVALID_INPUT')
                valid_id(project, 'species', e['value'])
                put(20+24*TIMES.index(e['time'])+2*e['slot'], struct.pack('<H', e['value']))
        else:
            offset, count, _ = METHODS[method]
            require(set(e) == common | {'slot'} and field in ('species', 'levels'), 'Invalid water/rock slot fields', 'INVALID_INPUT')
            integer(e['slot'], 0, count-1, 'Slot'); offset += 4*e['slot']
            if field == 'species':
                valid_id(project, 'species', e['value']); put(offset+2, struct.pack('<H', e['value']))
            else:
                v = e['value']
                require(isinstance(v, list) and len(v) == 2, 'Use [minimum, maximum] levels', 'INVALID_INPUT')
                for level in v:
                    integer(level, 1, 100, 'Level')
                require(v[0] <= v[1], 'Minimum level exceeds maximum', 'INVALID_INPUT'); put(offset, bytes(v))
    # Turning on a method must not activate malformed/empty slots.
    for method in {e['method'] for e in edits}:
        view = decode_wild(result)[method]
        if view['rate']:
            if method == 'grass':
                for level in view['levels']:
                    integer(level, 1, 100, 'Active grass level')
                ids = sum([view[t] for t in TIMES], [])
            else:
                ids = [s['species'] for s in view['slots']]
                for s in view['slots']:
                    integer(s['min_level'], 1, 100, 'Active minimum level'); integer(s['max_level'], s['min_level'], 100, 'Active maximum level')
            for species in ids:
                valid_id(project, 'species', species)
    return bytes(result)


def binding(project, state, header):
    qualify(project)
    require(type(header) is int and header == 34, 'This gameplay profile qualifies Route 30 (header 34)', 'UNSUPPORTED_CONTEXT')
    h = project.header(header)
    require([h['wild_pokemon'], h['event_file'], h['script_file'], h['text_archive']] == [3,31,227,375],
            'Route 30 resources were rebound', 'CONTEXT_MISMATCH')
    raw = event_authoring.raw_member(project, 31, state)
    from .formats import events
    npcs = [n for n in events(raw)['npcs'] if 3000 <= n['script'] < 3737]
    deps = {'baseline': baseline_digest(project), 'header': h['hex'], 'event': digest(raw),
            'common_script': digest(base(project, text.SCRIPT_ARCHIVE, 953)),
            'catalogs': {k: digest(archive(project, p)) for k, (p, _, _, _) in CATALOGS.items()}}
    return h, npcs, deps


def plan_v1(project, state, index, header, operations):
    """The r37 Route 30 planner, unchanged, so v1 history replays exactly."""
    h, npcs, deps = binding(project, state, header)
    require(isinstance(operations, list) and 1 <= len(operations) <= 16, 'Use 1..16 gameplay operations', 'INVALID_INPUT')
    changes, previews, seen = [], [], set()
    for op in operations:
        require(isinstance(op, dict) and op.get('kind') in ('trainer', 'encounters'), 'Invalid gameplay operation', 'INVALID_INPUT')
        kind = op['kind']
        if kind == 'trainer':
            require(set(op) == {'kind','id','before_sha256','party'}, 'Invalid trainer request', 'INVALID_INPUT')
            tid = op['id']; integer(tid, 1, 737, 'Trainer ID')
            require(any(n['script']-2999 == tid for n in npcs), 'Trainer is not bound to this route', 'CONTEXT_MISMATCH')
            key = (kind, tid)
            oldh, oldp = (current(project, state, p, tid) for p in (TRAINERS, PARTIES))
            require(op['before_sha256'] == digest(oldh+oldp), 'Trainer before-value differs; inspect again', 'BEFORE_VALUE_MISMATCH')
            newh, newp = encode_team(project, oldh, oldp, op['party'])
            rows = [(TRAINERS, tid, oldh, newh), (PARTIES, tid, oldp, newp)]
            previews.append({'kind':kind,'id':tid,'name':name(project,729,tid),'before':team_value(decode_team(oldh,oldp)),
                             'after':team_value(decode_team(newh,newp)),'policy':POLICY,
                             'preserved':'Class, trainer items, AI/battle flags; existing row difficulty, override and capsule.'})
        else:
            require(set(op) == {'kind','before_sha256','edits'}, 'Invalid encounter request', 'INVALID_INPUT')
            member = h['wild_pokemon']; key = (kind, member)
            old = current(project, state, WILD, member)
            require(op['before_sha256'] == digest(old), 'Encounter before-value differs; inspect again', 'BEFORE_VALUE_MISMATCH')
            new = encode_wild(project, old, op['edits']); rows = [(WILD, member, old, new)]
            previews.append({'kind':kind,'member':member,'edits':copy.deepcopy(op['edits']),
                             'before':decode_wild(old),'after':decode_wild(new)})
        require(key not in seen, 'Duplicate gameplay target in batch', 'DUPLICATE_EDIT'); seen.add(key)
        changes.extend({'archive':p,'member':i,'before':a.hex(),'after':b.hex()} for p,i,a,b in rows if a != b)
    context = authoring.context_ref(project.context(header=34, cell=[17,10]))
    return {'schema':SCHEMA,'index':index,'context':context,'request':{'header':header,'operations':copy.deepcopy(operations)},
            'dependencies':deps,'changes':changes,'preview':previews,'label':'Route 30 teams and encounters'}


def header_context(project, header):
    """A stable context reference for a header: its single cell, else its first cell."""
    try:
        return authoring.context_ref(project.context(header=header))
    except EditorError as exc:
        require(exc.code == 'CONTEXT_AMBIGUOUS', str(exc), exc.code)
    grid = project.matrix_data(project.header(header)['matrix'])
    cells = [c for c in world.matrix_cells(grid) if not grid['has_headers'] or c['header'] == header]
    return authoring.context_ref(project.context(header=header, cell=cells[0]['cell']))


def wild_users(project):
    cache = getattr(project, '_gameplay_wild_users', None)
    signature = project.world_signature()
    if cache is None or cache[0] != signature:
        users = {}
        for h in range(project.header_count()):
            users.setdefault(project.header(h)['wild_pokemon'], []).append(h)
        cache = project._gameplay_wild_users = (signature, users)
    return cache[1]


def area(project, state, header):
    """Resources of one header and what can be authored there, with explicit reasons."""
    qualify(project)
    require(type(header) is int and not isinstance(header, bool), 'Choose a map header', 'INVALID_INPUT')
    h = project.header(header)
    from .area_layout import resource_users
    count = struct.unpack_from('<H', archive(project, WILD), 24)[0]
    info = {'header': header, 'name': h['name'], 'hex': h['hex'], 'wild_member': h['wild_pokemon'],
            'event_member': h['event_file'], 'script_member': h['script_file'], 'text_member': h['text_archive']}
    info['encounter_users'] = wild_users(project).get(h['wild_pokemon'], [])
    if h['wild_pokemon'] >= count and not project.created_member(WILD, h['wild_pokemon']):
        info['encounters'] = False; info['encounter_reason'] = 'This area has no wild encounter table'
    else:
        raw = current(project, state, WILD, h['wild_pokemon'])
        info['encounters'] = len(raw) == 196
        info['encounter_reason'] = '' if info['encounters'] else 'Unsupported encounter record size'
    try:
        raw_events = event_authoring.raw_member(project, h['event_file'], state)
        from .formats import events
        npcs = [n for n in events(raw_events)['npcs'] if 3000 <= n['script'] < 3737]
        info['event_reason'] = ''
    except EditorError as exc:
        npcs = []; info['event_reason'] = str(exc)
    users = resource_users(project, {'header': h, 'event_member': h['event_file'], 'map_member': None})
    info['event_users'] = users['events']; info['script_users'] = users['scripts']
    return h, npcs, info


def areas(project, search='', offset=0, limit=40):
    """Compatible-area discovery across every header, without writes."""
    qualify(project)
    require(isinstance(search, str) and type(offset) is int and offset >= 0 and type(limit) is int
            and 1 <= limit <= 400, 'Invalid area query', 'INVALID_INPUT')
    state = project.composed(); rows = []
    for header in range(project.header_count()):
        name_ = project.header(header)['name']
        if search and search.lower() not in name_.lower() and search != str(header):
            continue
        rows.append(header)
    result = []
    for header in rows[offset:offset+limit]:
        h, npcs, info = area(project, state, header)
        trainers = sorted({n['script'] - 2999 for n in npcs})
        result.append({'header': header, 'name': info['name'], 'wild_member': info['wild_member'],
                       'encounters': info['encounters'], 'encounter_reason': info['encounter_reason'],
                       'encounter_shared_with': [u for u in info['encounter_users'] if u != header] if info['encounters'] else [],
                       'event_member': info['event_member'], 'event_shared_with': [u for u in info['event_users'] if u != header],
                       'trainers': trainers, 'trainer_reason': info['event_reason'] or ('' if trainers else 'No stock trainer scripts in this area')})
    return {'total': len(rows), 'offset': offset, 'areas': result,
            'notes': ['Encounter files and trainer records are shared data: editing one changes every listed user.',
                      'Areas without an encounter table or stock trainer scripts are listed with their reason.']}


def inspect(project, header=34):
    """Teams and encounters for one header (v1 shape; any compatible header)."""
    state = project.composed(); h, npcs, info = area(project, state, header)
    trainers = []
    for n in npcs:
        tid = n['script'] - 2999
        hd, party = (current(project, state, p, tid) for p in (TRAINERS, PARTIES))
        row = {'id': tid, 'name': name(project, 729, tid), 'npc_id': n['id'], 'position': [n['x'], n['z']],
               'before_sha256': digest(hd+party), 'policy': POLICY}
        try:
            rows = decode_team(hd, party); row['party'] = rows
            encode_team_v3(project, hd, party, team_value_v3(rows))
            row['editable'] = True
        except EditorError as exc:
            row.update(editable=False, reason=str(exc))
        trainers.append(row)
    result = {'revision': project.doc['revision'], 'header': header, 'route': info['name'], 'trainers': trainers,
              'resources': {k: info[k] for k in ('wild_member', 'event_member', 'encounter_users', 'event_users')}}
    if info['encounters']:
        raw = current(project, state, WILD, h['wild_pokemon'])
        result['encounters'] = {'archive': WILD, 'member': h['wild_pokemon'], 'before_sha256': digest(raw),
                                'shared_with': [u for u in info['encounter_users'] if u != header],
                                'methods': decode_wild(raw), 'notes': ['Grass levels are shared across all times.',
                                'Stored rates are modified by the engine; slot weights are not editable.',
                                'Radio, swarm and special replacements are preserved.']}
    else:
        result['encounters'] = None; result['encounter_reason'] = info['encounter_reason']
    return result


def species_data(project, species_id, form=0):
    from . import species, species_forms as sf
    qualify(project)
    state = project.composed()
    if form == 0 and type(species_id) is int and species_id <= 493:
        result = species.view(project, state, species_id)
        result['evolution_v3'] = species.decode_evolutions_v3(species.members(project, state, species_id)['evolution'])
    else:
        row = sf.require_supported(project, species_id, form)
        index = row['personal_index']
        if row.get('kind') == 'custom form':
            # A custom identity's records are its base species' own at export (inherited data).
            parts = species_rows(project, lambda a, m: current(project, state, a, m), row['inherits_from'])
            result = {'species': species_id, 'form': form, 'personal_index': index, 'name': row['name'],
                      'before_sha256': species.fingerprint(parts), 'editable': {}, 'follower': row['follower'],
                      'inherited_from': row['inherits_from'], 'identity': row['identity'],
                      'reasons': {k: 'inherited from the base species until its own design is chosen'
                                  for k in ('personal', 'learnset', 'evolution_v3', 'machines')}}
            result['personal'] = species.decode_personal(parts['personal'])
            result['learnset'] = species.decode_learnset(parts['learnset'])
            result['revision'] = project.doc['revision']
            return result
        parts = species_rows(project, lambda a, m: current(project, state, a, m), index)
        result = {'species': species_id, 'form': form, 'personal_index': index, 'name': row['name'],
                  'before_sha256': species.fingerprint(parts), 'editable': {}, 'reasons': {}, 'follower': row['follower']}
        for key, decode in (('personal', species.decode_personal), ('learnset', species.decode_learnset),
                            ('evolution_v3', species.decode_evolutions_v3), ('machines', species.decode_machines)):
            try:
                result[key] = decode(parts[key.split('_')[0]]); result['editable'][key] = True
            except Exception as exc:
                result['editable'][key] = False; result['reasons'][key] = str(exc)
        if 'machines' in result:
            result['machines'] = {'base_compatible': [i for i in result['machines'] if i in species.BASE_MACHINES],
                                  'other_bits_set': len([i for i in result['machines'] if i not in species.BASE_MACHINES])}
    result['revision'] = project.doc['revision']
    return result


def plan(project, state, index, header, operations):
    """v2: trainer, encounter and species operations as one atomic transaction."""
    from . import species
    qualify(project)
    require(isinstance(operations, list) and 1 <= len(operations) <= 32, 'Use 1..32 gameplay operations', 'INVALID_INPUT')
    changes, previews, seen, headers = [], [], set(), {}
    working = {}

    def now(path, member):
        return working.get((path, member), current(project, state, path, member))

    def whole(path, member, before, after):
        if before != after:
            changes.append({'archive': path, 'member': member, 'before': before.hex(), 'after': after.hex()})
            working[(path, member)] = after

    def rows(path, member, offset, before, after):
        if before != after:
            changes.append({'archive': path, 'member': member, 'offset': offset, 'before': before.hex(), 'after': after.hex()})
            raw = now(path, member)
            working[(path, member)] = raw[:offset] + after + raw[offset+len(after):]

    def claim(key):
        # Shared records are claimed before before-values: a second edit of the
        # same trainer, encounter file or species (via any header) is refused.
        require(key not in seen, 'Duplicate gameplay target in batch', 'DUPLICATE_EDIT'); seen.add(key)
        return key

    def bound(h):
        if h not in headers:
            headers[h] = area(project, state, h)
        return headers[h]
    for op in operations:
        require(isinstance(op, dict) and op.get('kind') in ('trainer', 'encounters', 'species'), 'Invalid gameplay operation', 'INVALID_INPUT')
        kind = op['kind']
        if kind == 'trainer':
            require(set(op) in ({'kind','id','before_sha256','party'}, {'kind','header','id','before_sha256','party'}),
                    'Invalid trainer request', 'INVALID_INPUT')
            where = op.get('header', header); h, npcs, info = bound(where)
            tid = op['id']; integer(tid, 1, 737, 'Trainer ID')
            require(any(n['script']-2999 == tid for n in npcs), 'Trainer is not bound to this area', 'CONTEXT_MISMATCH')
            key = claim((kind, tid))
            oldh, oldp = now(TRAINERS, tid), now(PARTIES, tid)
            require(op['before_sha256'] == digest(oldh+oldp), 'Trainer before-value differs; inspect again', 'BEFORE_VALUE_MISMATCH')
            newh, newp = encode_team(project, oldh, oldp, op['party'])
            whole(TRAINERS, tid, oldh, newh); whole(PARTIES, tid, oldp, newp)
            previews.append({'kind':kind,'header':where,'id':tid,'name':name(project,729,tid),'before':team_value(decode_team(oldh,oldp)),
                             'after':team_value(decode_team(newh,newp)),'policy':POLICY,
                             'preserved':'Class, trainer items, AI/battle flags; existing row difficulty, override and capsule.'})
        elif kind == 'encounters':
            require(set(op) in ({'kind','before_sha256','edits'}, {'kind','header','before_sha256','edits'}),
                    'Invalid encounter request', 'INVALID_INPUT')
            where = op.get('header', header); h, npcs, info = bound(where)
            require(info['encounters'], info['encounter_reason'] or 'Encounters are unsupported here', 'UNSUPPORTED_CONTEXT')
            member = h['wild_pokemon']; key = claim((kind, member))
            old = now(WILD, member)
            require(op['before_sha256'] == digest(old), 'Encounter before-value differs; inspect again', 'BEFORE_VALUE_MISMATCH')
            new = encode_wild(project, old, op['edits']); whole(WILD, member, old, new)
            previews.append({'kind':kind,'header':where,'member':member,'edits':copy.deepcopy(op['edits']),
                             'shared_with':[u for u in info['encounter_users'] if u != where],
                             'before':decode_wild(old),'after':decode_wild(new)})
        else:
            require(set(op) == {'kind','id','before_sha256','changes'}, 'Invalid species request', 'INVALID_INPUT')
            sid = op['id']; valid_id(project, 'species', sid); key = claim((kind, sid))
            parts = {'personal': now(species.PERSONAL, sid), 'evolution': now(species.EVOLUTION, sid),
                     'learnset': now(species.LEVELUP, 0)[sid*species.ROW_BYTES:(sid+1)*species.ROW_BYTES],
                     'machines': now(species.ADDONS, species.MACHINE_MEMBER)[sid*species.MACHINE_BYTES:(sid+1)*species.MACHINE_BYTES]}
            require(op['before_sha256'] == species.fingerprint(parts), 'Species before-value differs; inspect again', 'BEFORE_VALUE_MISMATCH')
            new, advisories = species.encode(project, parts, sid, op['changes'])
            whole(species.PERSONAL, sid, parts['personal'], new['personal'])
            whole(species.EVOLUTION, sid, parts['evolution'], new['evolution'])
            rows(species.LEVELUP, 0, sid*species.ROW_BYTES, parts['learnset'], new['learnset'])
            rows(species.ADDONS, species.MACHINE_MEMBER, sid*species.MACHINE_BYTES, parts['machines'], new['machines'])
            def shown(p):
                v = {'personal': species.decode_personal(p['personal']), 'learnset': species.decode_learnset(p['learnset']),
                     'evolutions': [e for e in species.decode_evolutions(p['evolution']) if e['method']],
                     'machines': [i for i in species.decode_machines(p['machines']) if i in species.BASE_MACHINES]}
                return v
            previews.append({'kind':kind,'id':sid,'name':entry(project,'species',sid)['name'],'changes':copy.deepcopy(op['changes']),
                             'before':shown(parts),'after':shown(new),'advisories':advisories})
    edited = sorted({p['id'] for p in previews if p['kind'] == 'species'})
    impact = species.impact(project, state, edited) if edited else None
    deps = {'baseline': baseline_digest(project),
            'catalogs': {k: digest(archive(project, p)) for k, (p, _, _, _) in CATALOGS.items()},
            'headers': {str(h): info[2]['hex'] for h, info in sorted(headers.items())}}
    if edited:
        deps['species_archives'] = {p: digest(archive(project, p)) for p in (species.PERSONAL, species.LEVELUP, species.EVOLUTION, species.ADDONS)}
    context = header_context(project, header if header is not None else next(iter(headers), 34))
    label = 'Gameplay: ' + ', '.join(sorted({p['kind'] for p in previews}))
    result = {'schema':SCHEMA_V2,'index':index,'context':context,'request':{'header':header,'operations':copy.deepcopy(operations)},
              'dependencies':deps,'changes':changes,'preview':previews,'label':label}
    if impact:
        result['impact'] = impact
    return result


def replay(project, state, transaction, index):
    try:
        v1 = transaction.get('schema') == SCHEMA
        planner = {SCHEMA: plan_v1, SCHEMA_V2: plan, SCHEMA_V3: plan_v3}[transaction.get('schema')]
        expected = planner(project, state, index, **transaction['request'])
        require(transaction == expected, 'Gameplay before-values or dependencies differ', 'BEFORE_VALUE_MISMATCH')
        members = state.setdefault('gameplay_members', {})
        checks = state.setdefault('gameplay_checks', [])
        checks.append({'index': index, 'baseline': expected['dependencies']['baseline'],
                       'headers': {'34': expected['dependencies']['header']} if v1 else expected['dependencies']['headers']})
        for c in expected['changes']:
            key = (c['archive'], c['member'])
            if 'offset' in c:
                raw = members.get(key, base(project, *key)); after = bytes.fromhex(c['after'])
                members[key] = raw[:c['offset']] + after + raw[c['offset']+len(after):]
            else:
                members[key] = bytes.fromhex(c['after'])
    except (KeyError, TypeError, ValueError, struct.error) as exc:
        raise EditorError('INVALID_INPUT', 'Malformed gameplay transaction') from exc


def replacements(state):
    result = {}
    for (path, member), raw in state.get('gameplay_members', {}).items():
        result.setdefault(path, {})[member] = raw
    return result


BOUND_FIELDS = ('wild_pokemon', 'area_data', 'matrix', 'script_file', 'level_script', 'text_archive', 'event_file')


def _bound_header(expected, header_id, name):
    """Decode a saved 24-byte header dependency; anything else is a changed dependency."""
    try:
        raw = bytes.fromhex(expected) if type(expected) is str else None
    except ValueError:
        raw = None
    require(raw is not None and len(raw) == 24,
            f'Saved gameplay dependency for header {header_id} is malformed or changed', 'CONTEXT_MISMATCH')
    return world.decode_header(raw, header_id, name, None)


def validate(project, state):
    """Final state: the resources each transaction was bound to are still bound.

    Before-values and whole dependencies are verified by replay at each
    transaction's own index. Later event or species edits in the same areas are
    compatible; rebinding a header's encounter/event/script resources is not.
    """
    for check in state.get('gameplay_checks', []):
        require(check['baseline'] == baseline_digest(project), 'Gameplay baseline differs', 'CONTEXT_MISMATCH')
        for h, expected in check['headers'].items():
            current = project.header(int(h))
            if current['hex'] == expected:
                continue
            # Later identity edits (name, popup, music, weather, region, town map) keep the
            # resources this transaction was bound to; any rebinding still refuses.
            before = _bound_header(expected, int(h), current['name'])
            require(all(current[k] == before[k] for k in BOUND_FIELDS),
                    'Route resources changed after gameplay authoring', 'CONTEXT_MISMATCH')


def summary(t):
    result = {'operation':'gameplay.transaction','index':t['index'],'context':t['context'],
              'label':t['label'],'preview':copy.deepcopy(t['preview'])}
    if 'impact' in t:
        result['impact'] = copy.deepcopy(t['impact'])
    return result


# ---- v3: expanded species, regional forms and typed evolutions ----------------------
# v1/v2 transactions keep replaying under their original planners above. v3 accepts
# {species, form} references qualified by species_forms (ROM roster) in trainer teams,
# wild slots and species records (form rows at their personal index), plus the typed
# evolution methods of species.EVOLUTION_METHODS. Coverage: docs/ASSETS_GAMEPLAY_COVERAGE.md.
SCHEMA_V3 = 'sovereign-gameplay-v3'
SCHEMAS = (SCHEMA, SCHEMA_V2, SCHEMA_V3)


def ref_word(project, value, what='Species'):
    """A species reference (int base species or {species, form}) -> checked packed word."""
    from . import species_forms as sf
    if type(value) is int and not isinstance(value, bool):
        species, form = value, 0
    else:
        require(isinstance(value, dict) and set(value) <= {'species', 'form'} and 'species' in value,
                f'{what} is a species number or {{species, form}}', 'INVALID_INPUT')
        species, form = value['species'], value.get('form', 0)
    if form or type(species) is int and species > 493:
        sf.require_supported(project, species, form, what)
    else:
        valid_id(project, 'species', species)
    return sf.pack(species, form)


def word_ok(project, word):
    from . import species_forms as sf
    species, form = sf.unpack(word)
    return sf.lookup(project, species, form)['supported']


def word_name(project, word):
    from . import species_forms as sf
    species, form = sf.unpack(word)
    return sf.display(project, species, form) if species else None


def team_value_v3(rows):
    return [{'species': r['species'], 'form': r['form'], 'level': r['level'], 'moves': r['moves'],
             'held_item': r['held_item']} for r in rows]


def encode_team_v3(project, header, party, value):
    old = decode_team(header, party)
    for r in old:
        require(word_ok(project, r['species'] | r['form'] << 11),
                f"Existing row species {r['species']} form {r['form']} is outside the qualified roster", 'UNSUPPORTED_TRAINER')
        valid_id(project, 'items', r['held_item'])
        for move in r['moves'] or []:
            valid_id(project, 'moves', move)
    require(isinstance(value, list) and 1 <= len(value) <= 6, 'Team needs 1..6 Pokémon', 'INVALID_INPUT')
    modes = set(); words = []
    for m in value:
        require(isinstance(m, dict) and set(m) in ({'species', 'level', 'moves', 'held_item'},
                                                    {'species', 'form', 'level', 'moves', 'held_item'}),
                'Invalid team fields', 'INVALID_INPUT')
        words.append(ref_word(project, {'species': m['species'], 'form': m.get('form', 0)}, 'Trainer Pokémon'))
        valid_id(project, 'items', m['held_item']); integer(m['level'], 1, 100, 'Level')
        moves = m['moves']; modes.add(moves is None)
        if moves is not None:
            require(isinstance(moves, list) and len(moves) == 4 and any(moves), 'Choose four move slots, at least one nonempty', 'INVALID_INPUT')
            for move in moves:
                valid_id(project, 'moves', move)
            require(len([v for v in moves if v]) == len(set(v for v in moves if v)), 'Duplicate moves are unsupported', 'INVALID_INPUT')
    require(len(modes) == 1, 'Default/custom moves is shared by the whole trainer team', 'INVALID_INPUT')
    h = bytearray(header)
    h[0] = (1 if value[0]['moves'] is not None else 0) | (2 if header[0] & 2 or any(m['held_item'] for m in value) else 0)
    h[3] = len(value)
    result = bytearray()
    for i, (m, word) in enumerate(zip(value, words)):
        opaque = old[i] if i < len(old) else {'difficulty': 0, 'ability_override': 0, 'capsule': 0}
        result.extend(struct.pack('<BBHH', opaque['difficulty'], opaque['ability_override'], m['level'], word))
        if h[0] & 2:
            result.extend(struct.pack('<H', m['held_item']))
        if h[0] & 1:
            result.extend(struct.pack('<4H', *m['moves']))
        result.extend(struct.pack('<H', opaque['capsule']))
    return bytes(h), bytes(result)


def encode_wild_v3(project, raw, edits):
    """As encode_wild, but species values may name a qualified expanded species or form."""
    decode_wild(raw)
    require(isinstance(edits, list) and 1 <= len(edits) <= 100, 'Use 1..100 encounter edits', 'INVALID_INPUT')
    result = bytearray(raw); touched = set()

    def put(offset, data):
        cells = set(range(offset, offset + len(data)))
        require(not cells & touched, 'An encounter field is edited twice', 'DUPLICATE_EDIT')
        touched.update(cells); result[offset:offset + len(data)] = data
    for e in edits:
        require(isinstance(e, dict) and e.get('method') in ('grass', *METHODS), 'Unknown encounter method', 'INVALID_INPUT')
        method, field = e['method'], e.get('field')
        common = {'method', 'field', 'value'}
        if field == 'rate':
            require(set(e) == common, 'Invalid rate fields', 'INVALID_INPUT')
            integer(e['value'], 0, 100, 'Stored encounter rate')
            put(0 if method == 'grass' else METHODS[method][2], bytes([e['value']]))
        elif method == 'grass':
            integer(e.get('slot'), 0, 11, 'Grass slot')
            if field == 'level':
                require(set(e) == common | {'slot'}, 'Grass levels are shared across all three times', 'INVALID_INPUT')
                integer(e['value'], 1, 100, 'Level'); put(8 + e['slot'], bytes([e['value']]))
            else:
                require(field == 'species' and set(e) == common | {'slot', 'time'} and e['time'] in TIMES,
                        'Choose grass species and morning/day/night', 'INVALID_INPUT')
                put(20 + 24 * TIMES.index(e['time']) + 2 * e['slot'], struct.pack('<H', ref_word(project, e['value'], 'Wild Pokémon')))
        else:
            offset, count, _ = METHODS[method]
            require(set(e) == common | {'slot'} and field in ('species', 'levels'), 'Invalid water/rock slot fields', 'INVALID_INPUT')
            integer(e['slot'], 0, count - 1, 'Slot'); offset += 4 * e['slot']
            if field == 'species':
                put(offset + 2, struct.pack('<H', ref_word(project, e['value'], 'Wild Pokémon')))
            else:
                v = e['value']
                require(isinstance(v, list) and len(v) == 2, 'Use [minimum, maximum] levels', 'INVALID_INPUT')
                for level in v:
                    integer(level, 1, 100, 'Level')
                require(v[0] <= v[1], 'Minimum level exceeds maximum', 'INVALID_INPUT'); put(offset, bytes(v))
    for method in {e['method'] for e in edits}:
        view = decode_wild(result)[method]
        if view['rate']:
            if method == 'grass':
                for level in view['levels']:
                    integer(level, 1, 100, 'Active grass level')
                ids = sum([view[t] for t in TIMES], [])
            else:
                ids = [s['species'] for s in view['slots']]
                for s in view['slots']:
                    integer(s['min_level'], 1, 100, 'Active minimum level'); integer(s['max_level'], s['min_level'], 100, 'Active maximum level')
            for word in ids:
                require(word_ok(project, word), f'Active slot species {word:#x} is outside the qualified roster', 'UNSUPPORTED_ID')
    return bytes(result)


def wild_names(project, view):
    names = {}
    words = sum((view['grass'][t] for t in TIMES), []) + [s['species'] for m in METHODS for s in view[m]['slots']]
    for w in set(words):
        if w:
            names[str(w)] = word_name(project, w)
    return names


def species_rows(project, now, index):
    from . import species as sp
    return {'personal': now(sp.PERSONAL, index), 'evolution': now(sp.EVOLUTION, index),
            'learnset': now(sp.LEVELUP, 0)[index * sp.ROW_BYTES:(index + 1) * sp.ROW_BYTES],
            'machines': now(sp.ADDONS, sp.MACHINE_MEMBER)[index * sp.MACHINE_BYTES:(index + 1) * sp.MACHINE_BYTES]}


def impact_v3(project, state, refs):
    """Users of changed records by exact packed word (species | form << 11)."""
    from . import species as sp
    wanted = set(refs); words = {s | f << 11 for s, f in refs}
    count = struct.unpack_from('<H', archive(project, TRAINERS), 24)[0]
    trainers = []
    for tid in range(1, count):
        try:
            rows = decode_team(current(project, state, TRAINERS, tid), current(project, state, PARTIES, tid))
        except Exception:
            continue
        hit = sorted({(r['species'], r['form']) for r in rows} & wanted)
        if hit:
            trainers.append({'id': tid, 'species': sorted({h[0] for h in hit}), 'refs': [list(h) for h in hit]})
    story = state.get('story', {}).get('trainer', {})
    authored = []
    for k, t in story.items():
        hit = sorted({(m['species'], m.get('form', 0)) for m in t['party']} & wanted)
        if hit:
            authored.append({'key': k, 'trainer_id': t['trainer_id'], 'species': sorted({h[0] for h in hit}),
                             'refs': [list(h) for h in hit]})
    encounters = []
    wild_count = struct.unpack_from('<H', archive(project, WILD), 24)[0]
    created = sorted(m for a, m in getattr(project, '_world_members', {}) if a == WILD)
    users = wild_users(project)
    for member in list(range(wild_count)) + created:
        raw = current(project, state, WILD, member)
        if len(raw) != 196:
            continue
        view = decode_wild(raw)
        ids = set(sum((view['grass'][t] for t in TIMES), [])) | {s['species'] for m in METHODS for s in view[m]['slots']}
        hit = sorted(ids & words)
        if hit:
            encounters.append({'member': member, 'headers': users.get(member, []), 'species': sorted({w & 0x7ff for w in hit}),
                               'refs': [[w & 0x7ff, w >> 11] for w in hit]})
    sources = []
    evo_count = struct.unpack_from('<H', archive(project, sp.EVOLUTION), 24)[0]
    for index in range(1, evo_count):
        raw = current(project, state, sp.EVOLUTION, index)
        if len(raw) != sp.EVOLUTION_SIZE:
            continue
        for slot in range(sp.EVOLUTION_SLOTS):
            method, param, target = struct.unpack_from('<3H', raw, slot * 6)
            if method and target in words:
                sources.append({'species': index, 'personal_index': index, 'slot': slot, 'method': method, 'param': param,
                                'target': target & 0x7ff, 'target_form': target >> 11})
    return {'trainers': trainers, 'authored_trainers': authored, 'encounter_files': encounters, 'evolution_sources': sources,
            'notes': ['Species records are global: every listed trainer, encounter and evolution uses the new values.',
                      'Stored party/box Pokémon keep their saved moves, ability, form and experience; use fresh Pokémon to inspect edits.',
                      'A regional form row is separate from its base species row.']}


def plan_v3(project, state, index, header, operations):
    """Trainer, encounter and species operations as one atomic v3 transaction."""
    from . import species as sp, species_forms as sf
    qualify(project)
    require(isinstance(operations, list) and 1 <= len(operations) <= 32, 'Use 1..32 gameplay operations', 'INVALID_INPUT')
    changes, previews, seen, headers = [], [], set(), {}
    working = {}

    def now(path, member):
        return working.get((path, member), current(project, state, path, member))

    def whole(path, member, before, after):
        if before != after:
            changes.append({'archive': path, 'member': member, 'before': before.hex(), 'after': after.hex()})
            working[(path, member)] = after

    def rows(path, member, offset, before, after):
        if before != after:
            changes.append({'archive': path, 'member': member, 'offset': offset, 'before': before.hex(), 'after': after.hex()})
            raw = now(path, member)
            working[(path, member)] = raw[:offset] + after + raw[offset + len(after):]

    def claim(key):
        require(key not in seen, 'Duplicate gameplay target in batch', 'DUPLICATE_EDIT'); seen.add(key)

    def bound(h):
        if h not in headers:
            headers[h] = area(project, state, h)
        return headers[h]
    edited = []
    for op in operations:
        require(isinstance(op, dict) and op.get('kind') in ('trainer', 'encounters', 'species'), 'Invalid gameplay operation', 'INVALID_INPUT')
        kind = op['kind']
        if kind == 'trainer':
            require(set(op) in ({'kind', 'id', 'before_sha256', 'party'}, {'kind', 'header', 'id', 'before_sha256', 'party'}),
                    'Invalid trainer request', 'INVALID_INPUT')
            where = op.get('header', header); h, npcs, info = bound(where)
            tid = op['id']; integer(tid, 1, 737, 'Trainer ID')
            require(any(n['script'] - 2999 == tid for n in npcs), 'Trainer is not bound to this area', 'CONTEXT_MISMATCH')
            claim((kind, tid))
            oldh, oldp = now(TRAINERS, tid), now(PARTIES, tid)
            require(op['before_sha256'] == digest(oldh + oldp), 'Trainer before-value differs; inspect again', 'BEFORE_VALUE_MISMATCH')
            newh, newp = encode_team_v3(project, oldh, oldp, op['party'])
            whole(TRAINERS, tid, oldh, newh); whole(PARTIES, tid, oldp, newp)
            after = team_value_v3(decode_team(newh, newp))
            previews.append({'kind': kind, 'header': where, 'id': tid, 'name': name(project, 729, tid),
                             'before': team_value_v3(decode_team(oldh, oldp)), 'after': after,
                             'names': [word_name(project, m['species'] | m['form'] << 11) for m in after], 'policy': POLICY,
                             'preserved': 'Class, trainer items, AI/battle flags; existing row difficulty, override and capsule.'})
        elif kind == 'encounters':
            require(set(op) in ({'kind', 'before_sha256', 'edits'}, {'kind', 'header', 'before_sha256', 'edits'}),
                    'Invalid encounter request', 'INVALID_INPUT')
            where = op.get('header', header); h, npcs, info = bound(where)
            require(info['encounters'], info['encounter_reason'] or 'Encounters are unsupported here', 'UNSUPPORTED_CONTEXT')
            member = h['wild_pokemon']; claim((kind, member))
            old = now(WILD, member)
            require(op['before_sha256'] == digest(old), 'Encounter before-value differs; inspect again', 'BEFORE_VALUE_MISMATCH')
            new = encode_wild_v3(project, old, op['edits']); whole(WILD, member, old, new)
            after = decode_wild(new)
            previews.append({'kind': kind, 'header': where, 'member': member, 'edits': copy.deepcopy(op['edits']),
                             'shared_with': [u for u in info['encounter_users'] if u != where],
                             'before': decode_wild(old), 'after': after, 'names': wild_names(project, after)})
        else:
            require(set(op) in ({'kind', 'id', 'before_sha256', 'changes'}, {'kind', 'id', 'form', 'before_sha256', 'changes'}),
                    'Invalid species request', 'INVALID_INPUT')
            sid, form = op['id'], op.get('form', 0)
            row = sf.require_supported(project, sid, form) if form or sid > 493 else None
            if row is None:
                valid_id(project, 'species', sid)
            pidx = sf.personal_index(project, sid, form)
            claim((kind, pidx))
            parts = species_rows(project, now, pidx)
            require(op['before_sha256'] == sp.fingerprint(parts), 'Species before-value differs; inspect again', 'BEFORE_VALUE_MISMATCH')
            require(isinstance(op['changes'], dict) and op['changes'], 'Choose species changes', 'INVALID_INPUT')
            simple = {k: v for k, v in op['changes'].items() if k != 'evolutions'}
            new, advisories = sp.encode(project, parts, pidx, simple) if simple else (dict(parts), [])
            if 'evolutions' in op['changes']:
                new['evolution'], extra = sp.encode_evolutions_v3(project, parts['evolution'], pidx, op['changes']['evolutions'])
                advisories += extra
            whole(sp.PERSONAL, pidx, parts['personal'], new['personal'])
            whole(sp.EVOLUTION, pidx, parts['evolution'], new['evolution'])
            rows(sp.LEVELUP, 0, pidx * sp.ROW_BYTES, parts['learnset'], new['learnset'])
            rows(sp.ADDONS, sp.MACHINE_MEMBER, pidx * sp.MACHINE_BYTES, parts['machines'], new['machines'])

            def shown(p):
                return {'personal': sp.decode_personal(p['personal']), 'learnset': sp.decode_learnset(p['learnset']),
                        'evolutions': [e for e in sp.decode_evolutions_v3(p['evolution']) if e['method_id']],
                        'machines': [i for i in sp.decode_machines(p['machines']) if i in sp.BASE_MACHINES]}
            edited.append((sid, form))
            previews.append({'kind': kind, 'id': sid, 'form': form, 'personal_index': pidx,
                             'name': sf.display(project, sid, form), 'changes': copy.deepcopy(op['changes']),
                             'before': shown(parts), 'after': shown(new), 'advisories': advisories})
    deps = {'baseline': baseline_digest(project),
            'catalogs': {k: digest(archive(project, p)) for k, (p, _, _, _) in CATALOGS.items()},
            'headers': {str(h): info[2]['hex'] for h, info in sorted(headers.items())}}
    if edited:
        deps['species_archives'] = {p: digest(archive(project, p)) for p in (sp.PERSONAL, sp.LEVELUP, sp.EVOLUTION, sp.ADDONS)}
    context = header_context(project, header if header is not None else next(iter(headers), 34))
    result = {'schema': SCHEMA_V3, 'index': index, 'context': context, 'request': {'header': header, 'operations': copy.deepcopy(operations)},
              'dependencies': deps, 'changes': changes, 'preview': previews,
              'label': 'Gameplay: ' + ', '.join(sorted({p['kind'] for p in previews}))}
    if edited:
        result['impact'] = impact_v3(project, state, edited)
    return result
