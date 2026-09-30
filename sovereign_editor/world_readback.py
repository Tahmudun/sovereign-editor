"""Independent candidate-ROM readback for created areas (no Project composition).

The ROM side is decoded from bytes alone: the runtime extension, internal-name
table, matrices, map members, events, scripts, text and encounter tables. The
expected side is the Project's composed state. Software evidence only; not
native acceptance.
"""

from . import world, world_runtime, dialogue_format as fmt, event_authoring as ev
from .formats import arm9_code, digest, events, file_span, member_count, resource

WILD = 'a/0/3/7'


def check(rom, project):
    state = project.composed()
    checks = []

    def add(name, condition, detail=None):
        checks.append({'check': name, 'pass': bool(condition), **({'detail': detail} if detail is not None else {})})

    ext = world_runtime.decode_extension(rom)
    created = sorted(project._world_headers)
    add('runtime extension present iff created headers exist', ext['present'] == bool(created))
    add('extension record count', ext['count'] == len(created), ext['count'])
    from . import petal_effect
    for i, h in enumerate(created):
        expected = petal_effect.header_raw(state, h, project._world_headers[h]['raw'])
        add(f'header {h} record', i < len(ext['records']) and ext['records'][i] == expected)
    stock = world.header_count(project.blob)
    add('internal-name table length', world.header_count(rom) == stock + len(created), world.header_count(rom))
    # The exported stock table, read once: every created header is checked against it.
    arm9 = arm9_code(rom)
    stock_heads = [world.read_header(rom, u, arm9) for u in range(stock)]
    for h in created:
        add(f'header {h} internal name', world.header_name(rom, h) == project._world_headers[h]['name'])
    _, base_names = file_span(project.blob, world.NAME_TABLE)
    _, names = file_span(rom, world.NAME_TABLE)
    add('stock internal names unchanged', names[:len(base_names)] == base_names)
    for i, h in enumerate(created):
        if i >= len(ext['records']):
            continue
        head = world.decode_header(ext['records'][i], h, world.header_name(rom, h), None)
        grid = world.decode_matrix(resource(rom, world.MATRIX_ARCHIVE, head['matrix'])[1], head['matrix'])
        area = next(a for a in state['world']['areas'].values() if a['header'] == h)
        add(f'header {h} matrix size', [grid['width'], grid['height']] == area['size'])
        for cell in area['cells']:
            x, y = cell['cell']
            member = grid['maps'][y][x]
            add(f'header {h} cell {x},{y} map member', member == cell['map_member'], member)
            ctx = project.context(header=h, cell=cell['cell'])
            expected = _map_bytes(project, ctx, state)
            add(f'header {h} cell {x},{y} map bytes', resource(rom, world.MAP_ARCHIVE, member)[1] == expected)
        raw_events = resource(rom, world.EVENT_ARCHIVE, head['event_file'])[1]
        decoded = events(raw_events)
        add(f'header {h} events', raw_events == ev.raw_member(project, head['event_file'], state),
            {k: len(v) for k, v in decoded.items()})
        for warp in decoded['warps']:
            target = _rom_header(rom, ext, warp['destination'], stock_heads)
            target_events = events(resource(rom, world.EVENT_ARCHIVE, target['event_file'])[1])['warps']
            back = target_events[warp['destination_warp']] if warp['destination_warp'] < len(target_events) else None
            add(f'header {h} warp {warp["id"]} lands on a warp', back is not None)
        scripts = resource(rom, fmt.SCRIPT_ARCHIVE, head['script_file'])[1]
        add(f'header {h} local scripts parse', _parses(fmt.script_entries, scripts))
        init = resource(rom, fmt.SCRIPT_ARCHIVE, head['level_script'])[1]
        add(f'header {h} map-load script is a valid table', _init_ok(init))
        text = resource(rom, fmt.TEXT_ARCHIVE, head['text_archive'])[1]
        add(f'header {h} text parses', _parses(fmt.text_entries, text))
        if head['wild_pokemon'] != 255:
            wild = resource(rom, WILD, head['wild_pokemon'])[1]
            from . import gameplay
            add(f'header {h} private encounter table', len(wild) == 196
                and wild == gameplay.current(project, state, WILD, head['wild_pokemon']))
            users = [u for u in range(stock) if stock_heads[u]['wild_pokemon'] == head['wild_pokemon']]
            add(f'header {h} encounter table not shared by stock headers', not users, users)
        owners = [u for u in range(stock) if head['event_file'] == stock_heads[u]['event_file']
                  or head['script_file'] == stock_heads[u]['script_file']]
        add(f'header {h} logic not shared by stock headers', not owners, owners)
    for connection in state.get('world', {}).get('connections', []):
        for warp in connection['warps']:
            head = _rom_header(rom, ext, warp['header'], stock_heads)
            rows = events(resource(rom, world.EVENT_ARCHIVE, head['event_file'])[1])['warps']
            row = rows[warp['id']] if warp['id'] < len(rows) else None
            add(f'connection warp {warp["header"]}:{warp["id"]}', row is not None
                and (row['x'], row['z'], row['destination'], row['destination_warp'])
                == (warp['x'], warp['z'], warp['destination'], warp['destination_warp']))
    for archive in (world.EVENT_ARCHIVE, fmt.SCRIPT_ARCHIVE, fmt.TEXT_ARCHIVE, WILD, world.MAP_ARCHIVE, world.MATRIX_ARCHIVE):
        expected = member_count(project.blob, archive) + sum(a == archive for a, _ in project._world_members)
        if archive == world.MAP_ARCHIVE:
            expected = member_count(project.blob, archive) + len(project._room_members)
        if archive == world.MATRIX_ARCHIVE:
            expected = member_count(project.blob, archive) + len(project._room_matrices)
        add(f'{archive} member count', member_count(rom, archive) == expected, member_count(rom, archive))
    return {'rom_sha256': digest(rom), 'checks': checks,
            'passed': sum(c['pass'] for c in checks), 'failed': sum(not c['pass'] for c in checks),
            'scope': 'ROM-byte readback of created areas; CPU and native checks are separate'}


def _rom_header(rom, ext, header, stock_heads):
    stock = world_runtime.BASE_COUNT
    if header < stock:
        return stock_heads[header]
    return world.decode_header(ext['records'][header - stock], header, world.header_name(rom, header), None)


def _map_bytes(project, ctx, state):
    from . import interiors, terrain_authoring
    # Terrain authoring v1 rebuilds the sound-plate block and height table after permissions.
    return terrain_authoring.finalize(project, ctx['map_member'], interiors.map_bytes(project, ctx, state), state)


def _parses(reader, raw):
    try:
        reader(raw)
        return True
    except Exception:
        return False


def _init_ok(raw):
    from .scene_authoring import init_records
    return _parses(init_records, raw)
