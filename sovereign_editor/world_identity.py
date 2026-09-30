"""Created-area identity, Pokégear location, area animation and tree behavior (world integration v1).

Three authoring operations on existing created areas, each its own transaction schema:

* ``identity``: player-visible name (a new map section and location-name message), arrival
  popup style, day/night music, weather, region, Pokégear/Town Map position and label, and
  an explicit interior parent. The created header record changes with masked field writes
  (world.encode_header); the resident runtime materializes it through layout v3 overrides.
* ``animation``: map members of a created area that must not receive the area's index-bound
  texture animation, exactly as stock excludes filler members 208/210/211 (PROD-VIS-002).
* ``trees`` (expanded by Project into ordinary permission transactions): decorative forest,
  i.e. behavior 6 (Headbutt) tiles become ordinary blocked/unblocked tiles with their
  collision and appearance unchanged (WORLD-TREE-001).

Verified consumers and exact anchors are recorded in work/world-integration-v1/impl/LEDGER.md.
Creation transactions are never rewritten: identity is applied by later transactions, and
legacy histories replay with their recorded meaning. Project owns every write.
"""
import copy
import struct

from . import authoring, world, world_runtime, dialogue_format as fmt
from .formats import EditorError, baseline_digest, digest, member_count, require, resource

IDENTITY_SCHEMA = 'sovereign-world-identity-v1'
ANIMATION_SCHEMA = 'sovereign-world-animation-v1'
SCHEMAS = (IDENTITY_SCHEMA, ANIMATION_SCHEMA)

SECTION_TEXT = 279          # location names by map section (msg_0279)
DESCRIPTION_TEXT = 273      # Pokégear map strings; location descriptions are u8 indices
BLANK_DESCRIPTION = 9       # the stock blank Pokégear description entry
SECTION_LIMIT = 256         # u8 header field
DESCRIPTION_LIMIT = 256     # u8 location-spec field
NAME_LENGTH = 16            # longest stock location name; the popup window is 132 pixels
POPUP_ARCHIVE = 'a/1/6/3'   # gs_areawindow: two members (graphics, palette) per popup style
WEATHER_NAMES = {0: 'clear', 1: 'rain', 5: 'snow', 11: 'dark (Flash)', 13: 'low light'}
REGIONS = ('johto', 'kanto')
OUTDOOR_TYPES = (1, 2)      # city/town, route
GRID = {'x': (1, 45), 'y': (2, 17)}   # Pokégear scroll limits; y - 2 is the header's 6-bit axis
HEADBUTT_BEHAVIOR = 6
IDENTITY_FIELDS = {'area', 'name', 'share_name', 'popup', 'music', 'weather', 'region', 'town_map', 'parent', 'label'}


# ---- qualified value sets (derived from the pinned baseline, cached per ROM) ---------------

def qualified(project):
    """Values stock headers use, plus resource-backed limits. Read-only and cached."""
    cache = project.__dict__.get('_identity_qualified')
    if cache is not None and cache[0] is project.blob:
        return cache[1]
    heads = [world.read_header(project.blob, h, project._base_arm9) for h in range(world.header_count(project.blob))]
    styles = member_count(project.blob, POPUP_ARCHIVE) // 2
    names_raw = resource(project.blob, fmt.TEXT_ARCHIVE, SECTION_TEXT)[1]
    descriptions_raw = resource(project.blob, fmt.TEXT_ARCHIVE, DESCRIPTION_TEXT)[1]
    stock_specs = world_runtime.stock_town_specs(project.blob)
    covered = {}
    for spec in stock_specs:
        map_id, x, y, bits = spec[:4]
        for xx in range(x, x + (bits & 15)):
            for yy in range(y, y + (bits >> 4 & 15)):
                covered.setdefault((xx, yy), map_id)
    result = {
        'music': sorted({h['music_day'] for h in heads} | {h['music_night'] for h in heads}),
        'weather': sorted({h['weather'] for h in heads}),
        'popup': [0] + [s for s in range(1, styles + 1) if s in {h['area_icon'] for h in heads}],
        'section_base': len(fmt.text_entries(names_raw)[1]),
        'description_base': len(fmt.text_entries(descriptions_raw)[1]),
        'stock_names': _stock_names(names_raw),
        'town_covered': covered,
        'main_matrix': world.read_matrix(project.blob, 0),
    }
    project._identity_qualified = (project.blob, result)
    return result


def _stock_names(raw):
    inverse = {v: k for k, v in fmt.CHARS.items()}
    names = []
    for i, (_, _, data) in enumerate(fmt.text_entries(raw)[1]):
        text = ''
        for j in range(len(data) // 2):
            c = struct.unpack_from('<H', data, 2 * j)[0] ^ (((i + 1) * 596947 + j * 18749) & 65535)
            if c == 0xFFFF:
                break
            text += inverse.get(c, '�')
        names.append(text)
    return names


def region_of(x, y):
    """Pokegear_RegionFromCoords (overlay 100) on the Pokégear grid: johto, kanto or indigo."""
    if x > 21:
        if x == 25 and y == 8:
            return 'johto'
        if (x == 28 and y == 6) or (x == 28 and 8 < y < 13):
            return 'indigo'
        return 'kanto'
    return 'johto'


# ---- composed state views ---------------------------------------------------------------

def world_state(state):
    return state.setdefault('world', {})


def identities(state):
    return state.get('world', {}).get('identity', {})


def sections(state):
    return state.get('world', {}).get('sections', {})


def descriptions(state):
    return state.get('world', {}).get('descriptions', {})


def static_members(state):
    return state.get('world', {}).get('static', {})


def _area(state, key):
    from . import world_authoring
    area = world_authoring.areas(state).get(key)
    require(area is not None, f'No created area {key!r}; identity edits apply to created areas only', 'NOT_FOUND')
    return area


# ---- text ----------------------------------------------------------------------------

def encode_name(text):
    require(isinstance(text, str) and 1 <= len(text.strip()) and len(text) <= NAME_LENGTH and text == text.strip(),
            f'A location name has 1..{NAME_LENGTH} characters without surrounding spaces', 'INVALID_INPUT')
    require('\n' not in text and '[' not in text, 'A location name is one plain line', 'INVALID_INPUT')
    fmt.encode_message(text)          # refuses characters the game font cannot show
    return text


def encode_description(text):
    require(isinstance(text, str) and 1 <= len(text) <= 57, 'A map description has 1..57 characters', 'INVALID_INPUT')
    require('[' not in text, 'Map descriptions are plain text', 'INVALID_INPUT')
    fmt.encode_message(text)          # two lines of at most 28 characters
    return text


def replacements(project, state):
    """Stock text members with appended location names and Pokégear descriptions (append-only:
    every stock message keeps its ID and encrypted bytes)."""
    result = {}
    names = sections(state)
    if names:
        q = qualified(project)
        ids = sorted(names)
        require(ids == list(range(q['section_base'], q['section_base'] + len(ids))), 'Map-section IDs must be contiguous',
                'RESOURCE_CONFLICT')
        raw = resource(project.blob, fmt.TEXT_ARCHIVE, SECTION_TEXT)[1]
        result[SECTION_TEXT] = fmt.append_messages(raw, [names[i]['name'] for i in ids], limit=SECTION_LIMIT)
    texts = descriptions(state)
    if texts:
        q = qualified(project)
        ids = sorted(texts)
        require(ids == list(range(q['description_base'], q['description_base'] + len(ids))),
                'Map description IDs must be contiguous', 'RESOURCE_CONFLICT')
        raw = resource(project.blob, fmt.TEXT_ARCHIVE, DESCRIPTION_TEXT)[1]
        result[DESCRIPTION_TEXT] = fmt.append_messages(raw, [texts[i]['text'] for i in ids], limit=DESCRIPTION_LIMIT)
    return {fmt.TEXT_ARCHIVE: result} if result else {}


# ---- identity -------------------------------------------------------------------------

def _grid_of(head):
    wm = head['worldmap']
    return None if (wm['x'], wm['y']) == (0, 0) else {'x': wm['x'], 'y': wm['y'] + 2}


def plan_identity(project, context, state, index, area, name=None, share_name=None, popup=None, music=None,
                  weather=None, region=None, town_map=None, parent=None, label=None, **unknown):
    """One identity transaction for an existing created area; fields left out keep their value.

    ``town_map`` is {'x', 'y'[, 'description']} on the Pokégear grid, or None to clear it (the
    marker then follows the special-spawn warp, as for stock rooms). ``parent`` names a
    created outdoor area whose marker and region an interior uses; ``share_name`` makes it
    use the parent's location name instead of its own.
    """
    require(not unknown, f'Unknown identity fields: {sorted(unknown)}', 'INVALID_INPUT')
    require(label is None or isinstance(label, str) and 0 < len(label) <= 160, 'Invalid identity label', 'INVALID_INPUT')
    require(isinstance(area, str), 'Choose a created area by its key', 'INVALID_INPUT')
    target = _area(state, area)
    header_id = target['header']
    require(context['header']['id'] == header_id, 'Open a cell of the area whose identity you edit', 'CONTEXT_MISMATCH')
    q = qualified(project)
    head = project.header(header_id)
    new = copy.deepcopy(head)
    current = copy.deepcopy(identities(state).get(area, {}))
    known = sections(state)
    sec = None
    changes = {}
    given = {k: v for k, v in dict(name=name, share_name=share_name, popup=popup, music=music, weather=weather,
                                   region=region, parent=parent).items() if v is not None}
    require(given or town_map is not None, 'Choose at least one identity field to change', 'INVALID_INPUT')

    # Parent (interior marker/region policy).
    parent_key = current.get('parent')
    if parent is not None:
        require(parent is False or isinstance(parent, str), 'parent is a created area key or false', 'INVALID_INPUT')
        parent_key = None if parent is False else parent
    if parent_key is not None:
        require(parent_key != area, 'An area cannot be its own parent', 'INVALID_INPUT')
        owner = _area(state, parent_key)
        parent_head = project.header(owner['header'])
        require(parent_head['location_type'] in OUTDOOR_TYPES, 'A parent must be an outdoor created area (town or route)',
                'INVALID_PARENT')
        require(head['location_type'] not in OUTDOOR_TYPES, 'Only interiors and caves take a parent; outdoor areas '
                'have their own town-map position', 'INVALID_PARENT')
        require(identities(state).get(parent_key, {}).get('parent') is None, 'Parent chains are not supported',
                'INVALID_PARENT')
        require(town_map is None and region is None, 'A parented area takes its marker and region from the parent',
                'INVALID_INPUT')
        require(_grid_of(parent_head) is not None, 'Give the parent a town-map position first', 'INVALID_PARENT')
        new['worldmap'] = {**new['worldmap'], 'x': parent_head['worldmap']['x'], 'y': parent_head['worldmap']['y']}
        new['kanto'] = parent_head['kanto']
    share = current.get('share_name', False) if share_name is None else share_name
    require(type(share) is bool, 'share_name is true or false', 'INVALID_INPUT')
    require(not share or parent_key is not None, 'Only a parented area can share its parent’s name', 'INVALID_INPUT')

    # Name -> map section (own, allocated once and then renamed in place; or the parent's).
    own = next((i for i, s in known.items() if s['owner'] == area), None)
    if share:
        require(name is None, 'A shared name comes from the parent; omit name', 'INVALID_INPUT')
        parent_section = next((i for i, s in known.items() if s['owner'] == parent_key), None)
        require(parent_section is not None, 'Give the parent its own location name first', 'INVALID_PARENT')
        new['location_name'] = parent_section
    elif name is not None:
        encode_name(name)
        clash = [i for i, s in known.items() if s['name'] == name and s['owner'] != area]
        require(not clash and name not in q['stock_names'], f'The location name {name!r} is already used', 'DUPLICATE_NAME')
        if own is None:
            own = q['section_base'] + len(known)
            require(own < SECTION_LIMIT, f'All {SECTION_LIMIT - q["section_base"]} new location names are allocated '
                    '(8-bit map section field)', 'RESOURCE_CAPACITY')
        sec = {'id': own, 'name': name, 'before': known.get(own, {}).get('name')}
        new['location_name'] = own
    elif share_name is False:
        require(own is not None, 'Give the area its own name when it stops sharing the parent\u2019s', 'INVALID_INPUT')
        new['location_name'] = own

    if popup is not None:
        require(type(popup) is int and popup in q['popup'], f'Popup style is one of {q["popup"]} (0 = no popup)',
                'INVALID_INPUT')
        new['area_icon'] = popup
    if music is not None:
        require(isinstance(music, dict) and set(music) == {'day', 'night'}, 'music needs day and night', 'INVALID_INPUT')
        for when in ('day', 'night'):
            require(type(music[when]) is int and music[when] in q['music'],
                    f'{when} music {music[when]!r} is not a stock field track', 'UNSUPPORTED_MUSIC')
        new['music_day'], new['music_night'] = music['day'], music['night']
    if weather is not None:
        require(type(weather) is int and weather in q['weather'],
                f'Weather is one of the stock field values {q["weather"]} ({WEATHER_NAMES})', 'UNSUPPORTED_WEATHER')
        new['weather'] = weather
    if region is not None:
        require(region in REGIONS, 'Region is johto or kanto', 'INVALID_INPUT')
        new['kanto'] = region == 'kanto'

    town = copy.deepcopy(current.get('town_map'))
    description = None
    if town_map is not None:
        require(parent_key is None, 'A parented area takes its marker from the parent', 'INVALID_INPUT')
        if town_map is False:
            town = None
            new['worldmap'] = {**new['worldmap'], 'x': 0, 'y': 0}
        else:
            require(isinstance(town_map, dict) and set(town_map) <= {'x', 'y', 'description'} and {'x', 'y'} <= set(town_map),
                    'town_map needs x, y and an optional description', 'INVALID_INPUT')
            x, y = town_map['x'], town_map['y']
            require(type(x) is int and type(y) is int and GRID['x'][0] <= x <= GRID['x'][1]
                    and GRID['y'][0] <= y <= GRID['y'][1],
                    f'Pokégear positions are x {GRID["x"][0]}..{GRID["x"][1]}, y {GRID["y"][0]}..{GRID["y"][1]} '
                    '(the header stores y - 2 in 6 bits; other raw values are not displayed)', 'UNSUPPORTED_POSITION')
            grid = q['main_matrix']
            require(x < grid['width'] and y - 2 < grid['height'], 'The marker cell must lie inside the main matrix',
                    'UNSUPPORTED_POSITION')
            require((x, y) not in q['town_covered'], f'Pokégear cell {x},{y} already labels stock header '
                    f'{q["town_covered"].get((x, y))}; choose a free cell', 'POSITION_CONFLICT')
            require(region_of(x, y) != 'indigo', 'Indigo Plateau cells are not supported', 'UNSUPPORTED_POSITION')
            town = {'x': x, 'y': y, 'description': town.get('description') if town else None,
                    'flavor': town.get('flavor') if town else None}
            if 'description' in town_map:
                text = town_map['description']
                if text is None:
                    town['description'], town['flavor'] = None, None
                else:
                    encode_description(text)
                    town['description'] = text
            new['worldmap'] = {**new['worldmap'], 'x': x, 'y': y - 2}
            new['kanto'] = region_of(x, y) == 'kanto' if region is None else new['kanto']
    if town is not None:
        require(region_of(town['x'], town['y']) == ('kanto' if new['kanto'] else 'johto'),
                'The region must match the Pokégear half that shows the marker', 'INVALID_REGION')
        for other, ident in identities(state).items():
            t = ident.get('town_map')
            require(other == area or not t or (t['x'], t['y']) != (town['x'], town['y']),
                    f'Pokégear cell {town["x"]},{town["y"]} already labels {other}', 'POSITION_CONFLICT')
        if town.get('description') is not None:
            texts = descriptions(state)
            mine = next((i for i, d in texts.items() if d['owner'] == area), None)
            if mine is None:
                mine = qualified(project)['description_base'] + len(texts)
                require(mine < DESCRIPTION_LIMIT, 'Pokégear descriptions are exhausted (8-bit field)', 'RESOURCE_CAPACITY')
            town['flavor'] = mine
            description = {'id': mine, 'text': town['description'], 'before': texts.get(mine, {}).get('text')}

    record = world.encode_header(new)
    after = world.decode_header(record, header_id, head['name'], None)
    changed = {k: [head[k], after[k]] for k in ('location_name', 'area_icon', 'music_day', 'music_night', 'weather',
                                                'kanto', 'worldmap') if head[k] != after[k]}
    identity = {'name': sec['name'] if sec else current.get('name') if not share else None,
                'section': after['location_name'], 'share_name': share, 'parent': parent_key,
                'popup': after['area_icon'], 'music': {'day': after['music_day'], 'night': after['music_night']},
                'weather': after['weather'], 'region': 'kanto' if after['kanto'] else 'johto',
                'town_map': town if parent_key is None else None}
    # Bookkeeping alone is not a change: something visible, a name/description or the
    # parent/share/town-map policy must differ.
    policy = (current.get('parent'), current.get('share_name', False), current.get('town_map'))
    require(changed or (sec is not None and sec['name'] != sec['before'])
            or (description is not None and description['text'] != description['before'])
            or (parent_key, share, identity['town_map']) != policy,
            'These identity values are already applied', 'NO_CHANGE')
    request = {k: copy.deepcopy(v) for k, v in dict(area=area, name=name, share_name=share_name, popup=popup, music=music,
               weather=weather, region=region, town_map=town_map, parent=parent, label=label).items() if v is not None}
    deps = {'baseline': baseline_digest(project), 'header_before': head['hex'],
            'sections': canonical_sections(state), 'descriptions': canonical_descriptions(state),
            'identity_before': current, 'applies_after': index}
    return {'schema': IDENTITY_SCHEMA, 'index': index, 'area': area, 'header': header_id,
            'context': authoring.context_ref(context), 'label': label or f'Identity: {area}', 'request': request,
            'before': head['hex'], 'after': record.hex(), 'changes': changed, 'section': sec,
            'description': description, 'identity': identity,
            'dependencies': deps, 'dependencies_sha256': authoring.canonical(deps)}


def canonical_sections(state):
    return authoring.canonical({str(k): v for k, v in sorted(sections(state).items())})


def canonical_descriptions(state):
    return authoring.canonical({str(k): v for k, v in sorted(descriptions(state).items())})


def register_identity(project, state, t):
    from . import world_authoring
    header = t['header']
    require(project._world_headers[header]['raw'].hex() == t['before'], 'Created header record changed', 'BEFORE_VALUE_MISMATCH')
    project._world_headers[header] = {**project._world_headers[header], 'raw': bytes.fromhex(t['after'])}
    world_authoring._invalidate(project)
    w = world_state(state)
    if t['section']:
        w.setdefault('sections', {})[t['section']['id']] = {'name': t['section']['name'], 'owner': t['area']}
    if t['description']:
        w.setdefault('descriptions', {})[t['description']['id']] = {'text': t['description']['text'], 'owner': t['area']}
    w.setdefault('identity', {})[t['area']] = copy.deepcopy(t['identity'])


# ---- animation (static members) --------------------------------------------------------

def animation_tracks(project, area_data):
    """Material slot names the area's texture animation binds by index (a/1/4/0 member)."""
    info = world.read_area_data(project.blob, area_data)
    member = info['dynamic_texture_type']
    if member == 0xFFFF:
        return []
    from . import nitro
    raw = resource(project.blob, 'a/1/4/0', member)[1]
    count = struct.unpack_from('<H', raw, 14)[0]
    offsets = struct.unpack_from(f'<{count}I', raw, 16)
    tracks = []
    for offset in offsets:
        require(raw[offset:offset + 4] == b'SRT0', 'Unsupported area texture animation', 'UNSUPPORTED_ANIMATION')
        block = raw[offset:offset + struct.unpack_from('<I', raw, offset + 4)[0]]
        names = nitro.info(block, 8)
        require(len(names) == 1, 'Unsupported area texture animation', 'UNSUPPORTED_ANIMATION')
        anim = block[struct.unpack('<I', names[0][1])[0]:]
        tracks = [n for n, _ in nitro.info(anim, 8, width=40)]
    return tracks


def material_slots(raw_model):
    from . import nitro
    blk = nitro.blocks(raw_model)[b'MDL0']
    entries = nitro.info(blk, 8)
    model = blk[struct.unpack('<I', entries[0][1])[0]:]
    _, _, mat, _, _ = struct.unpack_from('<5I', model)
    return [n for n, _ in nitro.info(model, mat + 4)]


def slot_mismatch(project, member, tracks):
    """Material slots whose index-bound animation track names a different material."""
    from .formats import map_data
    names = material_slots(map_data(project.member_raw(member))[2])
    return [{'slot': i, 'material': names[i], 'track': tracks[i]} for i in range(min(len(names), len(tracks)))
            if names[i] != tracks[i]]


def plan_animation(project, context, state, index, area, static, label=None, **unknown):
    """Mark created cells as static (no area texture animation), like stock filler members.

    ``static`` is 'auto' (every cell whose material slots do not match the area animation's
    track order) or an explicit list of [x, y] cells."""
    require(not unknown, f'Unknown animation fields: {sorted(unknown)}', 'INVALID_INPUT')
    require(label is None or isinstance(label, str) and 0 < len(label) <= 160, 'Invalid label', 'INVALID_INPUT')
    target = _area(state, area)
    require(context['header']['id'] == target['header'], 'Open a cell of that area', 'CONTEXT_MISMATCH')
    head = project.header(target['header'])
    tracks = animation_tracks(project, head['area_data'])
    cells = {tuple(c['cell']): c for c in target['cells']}
    report = []
    for pos, cell in sorted(cells.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        mismatch = slot_mismatch(project, cell['map_member'], tracks)
        report.append({'cell': list(pos), 'map_member': cell['map_member'], 'mismatched_slots': mismatch})
    if static == 'auto':
        chosen = [r['cell'] for r in report if r['mismatched_slots']]
    else:
        require(isinstance(static, list) and all(isinstance(c, list) and len(c) == 2 and tuple(c) in cells
                                                   for c in static), 'static lists [x, y] cells of the area', 'INVALID_INPUT')
        chosen = sorted({tuple(c) for c in static}, key=lambda c: (c[1], c[0]))
        chosen = [list(c) for c in chosen]
    require(tracks, 'This area has no texture animation; nothing to exclude', 'NO_CHANGE')
    before = sorted(static_members(state).get(area, []))
    members = sorted(cells[tuple(c)]['map_member'] for c in chosen)
    require(members != before, 'These cells already have this animation policy', 'NO_CHANGE')
    request = {'area': area, 'static': copy.deepcopy(static), **({'label': label} if label else {})}
    deps = {'baseline': baseline_digest(project), 'area_data': head['area_data'], 'tracks': tracks,
            'models': {str(r['map_member']): digest(project.member_raw(r['map_member'])) for r in report},
            'before': before, 'applies_after': index}
    return {'schema': ANIMATION_SCHEMA, 'index': index, 'area': area, 'header': target['header'],
            'context': authoring.context_ref(context), 'label': label or f'Static forest cells: {area}',
            'request': request, 'static_cells': chosen, 'members': members, 'review': report,
            'dependencies': deps, 'dependencies_sha256': authoring.canonical(deps)}


def register_animation(project, state, t):
    world_state(state).setdefault('static', {})[t['area']] = list(t['members'])


# ---- replay / summary ------------------------------------------------------------------

def plan(project, context, state, index, action, **request):
    if action == 'identity':
        return plan_identity(project, context, state, index, **request)
    return plan_animation(project, context, state, index, **request)


def replay(project, state, t, index):
    try:
        ref = t['context']
        ctx = project.context(header=ref['header'], cell=ref['cell'])
        request = {k: v for k, v in t['request'].items()}
        if t['schema'] == IDENTITY_SCHEMA:
            expected = plan_identity(project, ctx, state, index, **request)
        else:
            expected = plan_animation(project, ctx, state, index, **request)
        require(t == expected, 'Identity or animation before-values or dependencies differ', 'BEFORE_VALUE_MISMATCH')
        if t['schema'] == IDENTITY_SCHEMA:
            register_identity(project, state, t)
        else:
            register_animation(project, state, t)
        state['contexts'].append(ctx)
    except (KeyError, TypeError, ValueError, struct.error) as exc:
        raise EditorError('INVALID_INPUT', 'Malformed identity transaction') from exc


def summary(t):
    if t['schema'] == IDENTITY_SCHEMA:
        return {'operation': 'world.identity', 'index': t['index'], 'label': t['label'], 'context': t['context'],
                'area': t['area'], 'header': t['header'], 'changes': t['changes'], 'identity': t['identity'],
                'section': t['section'], 'description': t['description']}
    return {'operation': 'world.animation', 'index': t['index'], 'label': t['label'], 'context': t['context'],
            'area': t['area'], 'static_cells': t['static_cells'], 'members': t['members']}


# ---- whole-state validation and runtime extras ----------------------------------------

def validate(project, state):
    """Parents stay consistent with their interiors (stale links refuse); locations stay unique."""
    idents = identities(state)
    cells = {}
    for key, ident in idents.items():
        area = _area(state, key)
        head = project.header(area['header'])
        if ident.get('parent'):
            parent = _area(state, ident['parent'])
            parent_head = project.header(parent['header'])
            require((head['worldmap']['x'], head['worldmap']['y'], head['kanto'])
                    == (parent_head['worldmap']['x'], parent_head['worldmap']['y'], parent_head['kanto']),
                    f'{key} no longer matches its parent {ident["parent"]}’s marker/region; re-apply its identity',
                    'STALE_PARENT')
            if ident.get('share_name'):
                require(head['location_name'] == parent_head['location_name'],
                        f'{key} no longer shares {ident["parent"]}’s name; re-apply its identity', 'STALE_PARENT')
        town = ident.get('town_map')
        if town:
            require(_grid_of(head) == {'x': town['x'], 'y': town['y']}, f'{key} town-map record differs from its header',
                    'BEFORE_VALUE_MISMATCH')
            require((town['x'], town['y']) not in cells, f'Two areas label Pokégear cell {town["x"]},{town["y"]}',
                    'POSITION_CONFLICT')
            cells[(town['x'], town['y'])] = key
    if town_specs(project, state) or any(static_members(state).values()):
        require(len(town_specs(project, state)) <= world_runtime.MAX_TOWN_SPECS, 'Too many town-map locations',
                'RESOURCE_CAPACITY')


def children(state, parent):
    """Created areas whose identity names ``parent`` as their parent, in key order."""
    return sorted(k for k, ident in identities(state).items() if ident.get('parent') == parent and parent is not None)


def town_specs(project, state):
    """Stock-format 16-byte location specs for created outdoor areas with a town-map position."""
    specs = []
    for key, ident in sorted(identities(state).items(), key=lambda kv: _area(state, kv[0])['header']):
        town = ident.get('town_map')
        if not town or ident.get('parent'):
            continue
        header = _area(state, key)['header']
        flavor = town.get('flavor') if town.get('flavor') is not None else BLANK_DESCRIPTION
        specs.append(world_runtime.SPEC.pack(header, town['x'], town['y'], 0x11, flavor, 0, 0, 0, 0, 0))
    return specs


def static_list(state):
    return sorted({m for members in static_members(state).values() for m in members})


def extras_present(state):
    return bool(static_list(state) or any(i.get('town_map') for i in identities(state).values()))


def view(project, state=None):
    """Identity report for UI/CLI: every created area's player-visible identity and capacity."""
    from . import world_authoring
    state = project.composed() if state is None else state
    q = qualified(project)
    rows = []
    for key, area in sorted(world_authoring.areas(state).items(), key=lambda kv: kv[1]['header']):
        head = project.header(area['header'])
        ident = identities(state).get(key, {})
        section = head['location_name']
        name = (sections(state)[section]['name'] if section in sections(state)
                else q['stock_names'][section] if section < len(q['stock_names']) else None)
        rows.append({'area': key, 'header': area['header'], 'internal_name': area['internal_name'],
                     'project_name': area['name'], 'location_name': name, 'map_section': section,
                     'own_section': section in sections(state) and sections(state)[section]['owner'] == key,
                     'popup': head['area_icon'], 'music': {'day': head['music_day'], 'night': head['music_night']},
                     'weather': head['weather'], 'region': 'kanto' if head['kanto'] else 'johto',
                     'location_type': head['location_type'], 'town_map': _grid_of(head),
                     'parent': ident.get('parent'), 'share_name': ident.get('share_name', False),
                     'static_members': static_members(state).get(key, []),
                     'identity_edited': key in identities(state)})
    used = len(sections(state))
    return {'revision': project.doc['revision'], 'areas': rows,
            'capacity': {'location_names': {'used': used, 'limit': SECTION_LIMIT - q['section_base'],
                                            'first_id': q['section_base'],
                                            'refusal': 'identity name: 8-bit map section field and msg 279'},
                         'descriptions': {'used': len(descriptions(state)),
                                          'limit': DESCRIPTION_LIMIT - q['description_base']},
                         'town_map_locations': {'used': len(town_specs(project, state)),
                                                'limit': world_runtime.MAX_TOWN_SPECS},
                         'identity_overrides': 'one 12-byte resident record per distinct identity; see runtime report'},
            'valid_values': {'popup': q['popup'], 'weather': {str(k): WEATHER_NAMES.get(k, 'stock') for k in q['weather']},
                             'music': q['music'], 'region': list(REGIONS),
                             'town_map': {'x': list(GRID['x']), 'y': list(GRID['y']),
                                          'stock_cells_refused': len(q['town_covered'])}}}


# ---- trees (WORLD-TREE-001) --------------------------------------------------------------

TREE_FIELDS = {'action', 'area', 'policy', 'retain', 'label'}


def tree_request(state, request):
    require(set(request) <= TREE_FIELDS and request.get('policy') == 'decorative',
            'Trees take area, policy "decorative", optional retain and label', 'INVALID_INPUT')
    area = _area(state, request.get('area'))
    retain = request.get('retain') or []
    require(isinstance(retain, list) and all(isinstance(t, dict) and set(t) == {'x', 'z'} for t in retain),
            'retain lists {x, z} tiles', 'INVALID_INPUT')
    # A retained Headbutt tree makes the game read a/2/5/2[header]; created header IDs have
    # no qualified Headbutt table, so interactable trees are refused rather than left unsafe.
    require(not retain, f'Created header {area["header"]} has no qualified Headbutt encounter table; '
            'interactable trees are not supported on created areas', 'UNSUPPORTED_HEADBUTT')
    return area


def tree_cells(project, context, state, retain=()):
    """Behavior-6 (Headbutt) tiles of one cell as ordinary tiles; collision byte and visuals unchanged."""
    member = context['map_member']
    raw = project.member_raw(member)
    start = context['sections']['permissions_offset']
    ox, oz = context['origin']
    keep = {(t['x'], t['z']) for t in retain}
    cells = []
    for lz in range(world.MAP_SIZE):
        for lx in range(world.MAP_SIZE):
            offset = start + 2 * (lz * world.MAP_SIZE + lx)
            pair = state['permissions'].get((member, offset), raw[offset:offset + 2])
            if pair[0] == HEADBUTT_BEHAVIOR and (ox + lx, oz + lz) not in keep:
                cells.append({'x': ox + lx, 'z': oz + lz, 'before': pair.hex(), 'after': bytes((0, pair[1])).hex()})
    return cells


def headbutt_tiles(project, state):
    """Remaining behavior-6 tiles per created area (a created header has no Headbutt table)."""
    from . import world_authoring
    result = {}
    for key, area in world_authoring.areas(state).items():
        count = 0
        for cell in area['cells']:
            ctx = project.context(header=area['header'], cell=cell['cell'])
            count += len(tree_cells(project, ctx, state))
        if count:
            result[key] = count
    return result
