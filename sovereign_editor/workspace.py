"""Unified project workspace (WORKSPACE-01..03). Read-only; Project owns writes.

* ``index``: every authored resource with its cross-links: areas (created, and stock areas that
  hold authored content), characters, trainers, named states, events (story sequences and simple
  interactions), shops, edited gameplay records, custom assets (props), world connections and
  terrain features. ``uses`` are outgoing references; ``used_by`` is derived, so shared users of
  a resource are always visible.
* ``progression``: the warp / scripted-travel graph over project areas with findings graded
  ``error`` (definite: a missing endpoint), ``conditional`` (travel behind a requirement or
  state branch, reachable only after it is met), ``warning`` (an entrance no walkable arrival of
  its own area reaches under the composed collision) and ``unanalyzed`` (stock script logic the
  editor does not interpret). Static analysis never blocks export; valid scripts stay valid.
* ``impact``: the resources a staged batch changes and everything that shares them.
"""
import collections

from . import story_authoring as sa, world

KINDS = ('area', 'character', 'trainer', 'state', 'event', 'shop', 'record', 'asset', 'connection', 'terrain')
RECORD_ARCHIVES = {'a/0/1/1': 'move', 'a/0/1/7': 'item', 'a/0/0/2': 'species data', 'a/0/3/3': 'level-up moves',
                   'a/0/3/4': 'evolution', 'a/0/3/7': 'wild encounters', 'a/0/5/5': 'trainer header',
                   'a/0/5/6': 'trainer party'}
STATE_OPS = ('if', 'set')


def _ref(kind, key): return f'{kind}:{key}'


def _area_key(project, header):
    return f'h{header}'


def _event_uses(project, state, key, spec):
    uses = [('area', _area_key(project, spec['context']['header']))]
    if spec.get('character'):
        uses.append(('character', spec['character']))
    if spec.get('trainer'):
        uses.append(('trainer', spec['trainer']))
    for field in ('once_state',):
        if spec.get(field):
            uses.append(('state', spec[field]))
    if (spec.get('trigger') or {}).get('state'):
        uses.append(('state', spec['trigger']['state']))
    if (spec.get('field') or {}).get('state'):
        uses.append(('state', spec['field']['state']))
    for n in spec.get('nodes', []):
        op = n.get('op')
        if op in STATE_OPS or op == 'require' and n.get('kind') == 'state':
            uses.append(('state', n['state']))
        if op == 'battle':
            uses.extend(('trainer', n[k]) for k in ('trainer', 'partner', 'opponent2') if n.get(k))
        if op == 'require' and n.get('kind') == 'trainer':
            uses.append(('trainer', n['trainer']))
        if op == 'shop':
            uses.append(('shop', n['shop']))
        if op == 'warp':
            uses.append(('area', _area_key(project, n['header'])))
        if op in ('tutor',) and n.get('move'):
            uses.append(('record', f"move {n['move']}"))
    return uses


def index(project, state=None):
    """{ref: entry} for every authored resource, with ``uses`` and derived ``used_by``."""
    from . import world_authoring as wa, props, simple_interactions as si, terrain_authoring as ta, field_services as fs
    state = project.composed() if state is None else state
    entries = {}

    def add(kind, key, name, detail='', header=None, cell=None, uses=()):
        entries[_ref(kind, key)] = {'kind': kind, 'key': key, 'name': name, 'detail': detail, 'header': header,
                                    'cell': cell, 'uses': sorted({_ref(k, v) for k, v in uses}), 'used_by': []}

    created = {a['header']: (identity, a) for identity, a in wa.areas(state).items()}
    headers = set(created)
    for s in list(sa.catalog(state, 'sequence').values()) + list(si.specs(state).values()):
        headers.add(s['context']['header'])
    for c in state.get('world', {}).get('connections', []):
        for w in c['warps']:
            headers.update((w['header'], w['destination']))
    for inst in props.instances(state).values():
        headers.add(inst['context']['header'])
    for h in sorted(headers):
        if h in created:
            identity, a = created[h]
            cell = a['cells'][0]['cell'] if a.get('cells') else None
            add('area', _area_key(project, h), f"{a.get('name') or identity} (created, header {h})",
                f"{len(a.get('cells', []))} cell(s) · identity {identity}", h, cell)
        else:
            try:
                name = world.header_name(project.blob, h)
            except Exception:
                name = f'header {h}'
            add('area', _area_key(project, h), f'{name} (stock, header {h})', 'stock area with authored content', h)
    for key, c in sa.catalog(state, 'character').items():
        add('character', key, c.get('name', key), f"custom character · sprite package {c.get('sprite_sha256', '')[:8]}")
    for key, t in sa.catalog(state, 'trainer').items():
        uses = [('character', t['character'])] if t.get('character') else []
        if t.get('defeat_state'):
            uses.append(('state', t['defeat_state']))
        for m in t.get('party', []):
            for move in m.get('moves') or []:
                if move:
                    uses.append(('record', f'move {move}'))
            if m.get('held_item'):
                uses.append(('record', f"item {m['held_item']}"))
        policy = t.get('policy') or 'practice'
        add('trainer', key, t.get('name', key), f"{policy} · {len(t.get('party', []))} Pokémon"
            + (f" · {t.get('battle')}" if t.get('battle') else ''), uses=uses)
    for key, v in sa.catalog(state, 'state').items():
        detail = f"on/off flag {v['flag']:#x}" if v.get('switch') else f"number variable {v.get('variable', 0):#x}"
        add('state', key, v.get('name', key), detail)
    for key, s in sa.catalog(state, 'sequence').items():
        kind = 'field object ' + s['field']['family'] if s.get('field') else s['kind']
        add('event', key, key, f"{kind} at {s['x']},{s['z']} · {len(s.get('nodes', []))} step(s)",
            s['context']['header'], s['context']['cell'], _event_uses(project, state, key, s))
    for key, s in si.specs(state).items():
        add('event', key, key, f"{s['kind']} interaction at {s['x']},{s['z']}", s['context']['header'],
            s['context']['cell'], [('area', _area_key(project, s['context']['header']))])
    index_of = fs.shop_index(state)
    for name, items in fs.shops(state).items():
        add('shop', name, name, f"special mart {index_of[name]} · {len(items)} item(s)",
            uses=[('record', f'item {i}') for i in items])
    for (archive, member) in sorted(state.get('gameplay_members', {})):
        label = RECORD_ARCHIVES.get(archive, archive)
        key = f'{label} {member}'
        add('record', key, key, f'edited {archive} member {member}')
    for index_, move in sorted(state.get('machine_moves', {}).items()):
        add('record', f'TM{index_ + 1:03d}', f'TM{index_ + 1:03d}', f'teaches move {move}',
            uses=[('record', f'move {move}')])
    for key, a in props.assets(state).items():
        add('asset', key, a.get('display', key), f"custom prop · model {a.get('model_id')} · revision {a.get('revision')}")
    for key, inst in props.instances(state).items():
        add('asset', f'{key} (placement)', f"{key} placement", f"{inst['asset']} at {inst['x']},{inst['z']}",
            inst['context']['header'], inst['context']['cell'],
            [('asset', inst['asset']), ('area', _area_key(project, inst['context']['header']))])
    for c in state.get('world', {}).get('connections', []):
        heads = sorted({w['header'] for w in c['warps']} | {w['destination'] for w in c['warps']})
        add('connection', f"{c['index']}", c['label'], ', '.join(f"{w['header']}:{w['x']},{w['z']}→{w['destination']}"
                                                               for w in c['warps']),
            uses=[('area', _area_key(project, h)) for h in heads])
    seen = set()
    for items in state.get('terrain_features', {}).values():
        for f in items:
            if f['id'] in seen:
                continue
            seen.add(f['id'])
            add('terrain', f"{f['header']}:{f['id']}", f['id'], f"{f['spec']['action']} · ground {f['ground']}",
                f['header'], None, [('area', _area_key(project, f['header']))])
    for ref, e in list(entries.items()):
        for target in e['uses']:
            if target in entries:
                entries[target]['used_by'].append(ref)
            else:
                # A reference to something without its own entry (e.g. a stock record): add a stub.
                kind, key = target.split(':', 1)
                entries.setdefault(target, {'kind': kind, 'key': key, 'name': key, 'detail': 'stock (unedited)',
                                            'header': None, 'cell': None, 'uses': [], 'used_by': []})
                entries[target]['used_by'].append(ref)
    for e in entries.values():
        e['used_by'] = sorted(set(e['used_by']))
    return entries


def search(project, text='', kinds=None, limit=200, state=None):
    entries = index(project, state)
    needle = text.lower().strip()
    rows = [dict(ref=ref, **e) for ref, e in sorted(entries.items())
            if (not kinds or e['kind'] in kinds)
            and (not needle or needle in ref.lower() or needle in e['name'].lower() or needle in e['detail'].lower())]
    counts = collections.Counter(e['kind'] for e in entries.values())
    return {'revision': project.doc['revision'], 'total': len(rows), 'counts': dict(counts), 'entries': rows[:limit]}


def references(project, ref, state=None):
    entries = index(project, state)
    from .formats import require
    require(ref in entries, f'Unknown workspace resource {ref}', 'NOT_FOUND')
    e = entries[ref]
    brief = lambda r: {'ref': r, 'kind': entries[r]['kind'], 'name': entries[r]['name'], 'detail': entries[r]['detail']}
    return {'ref': ref, **{k: e[k] for k in ('kind', 'key', 'name', 'detail', 'header', 'cell')},
            'uses': [brief(r) for r in e['uses']], 'used_by': [brief(r) for r in e['used_by']],
            'shared': len(e['used_by']) > 1}


# ---- progression -----------------------------------------------------------------------------

def _warps(project, state, header):
    from . import event_authoring as ev
    head = project.header(header)
    return [r for r in ev.records(ev.raw_member(project, head['event_file'], state)) if r['kind'] == 'warp']


def _dead_entrances(project, state, header, warps):
    """Door / exit mat / gate / cave exit tiles of a created area that no warp uses (stepping on them
    leads nowhere): e.g. a template's unused door, or a carved exit left behind by a moved entrance."""
    from . import terrain_authoring as ta, world_authoring as wa
    _, area = ta.area_of(project, state, header)
    linked = {(w['x'], w['z']) for w in warps}
    dead = []
    for cell in area['cells']:
        ctx = project.context(header=header, cell=cell['cell'])
        ox, oz = ctx['origin']
        raw = project.member_raw(ctx['map_member'])
        for zz in range(oz, oz + 32):
            for xx in range(ox, ox + 32):
                offset = world.cell_offset(ctx, xx, zz)
                behavior, flags = state['permissions'].get((ctx['map_member'], offset), raw[offset:offset + 2])
                # Same qualification as a connection endpoint: the collision bit matches the kind
                # (a cave exit's hole behind the wall carries the exit-mat behavior but is solid).
                if behavior in wa.WARP_TILES and bool(flags & 0x80) == wa.WARP_TILES[behavior][1] \
                        and (xx, zz) not in linked:
                    dead.append((xx, zz, wa.WARP_TILES[behavior][0]))
    return dead


def _conditions(spec, target):
    """Requirement/branch steps on the path from the first step to ``target`` (by step id)."""
    nodes = {n['id']: n for n in spec.get('nodes', [])}
    if not nodes:
        return []
    first = spec['nodes'][0]['id']
    found = []

    def walk(label, path, seen):
        if label == target:
            found.append(list(path))
            return
        if label in seen or label not in nodes:
            return
        n = nodes[label]
        seen = seen | {label}
        for branch in ('next', 'yes', 'no', 'won', 'lost'):
            if n.get(branch):
                step = path
                if n['op'] in ('require', 'if') and branch in ('yes', 'no'):
                    what = (f"{n['kind']} {n.get(n['kind'], n.get('state', ''))}" if n['op'] == 'require'
                            else f"state {n['state']} = {n['value']}")
                    step = path + [f"{what} ({branch})"]
                elif n['op'] == 'choice' and branch == 'no':
                    continue
                walk(n[branch], step, seen)
    walk(first, [], frozenset())
    return min(found, key=len) if found else ['unreachable step']


def progression(project, state=None, flood=True):
    """Areas, warp/travel edges and graded findings for the project's areas."""
    from . import world_authoring as wa
    state = project.composed() if state is None else state
    count = world.header_count(project.blob) + len(project._world_headers)
    entries = index(project, state)
    scope = sorted(e['header'] for e in entries.values() if e['kind'] == 'area' and e['header'] is not None)
    created = {a['header'] for a in wa.areas(state).values()}
    areas, edges, findings, dead = [], [], [], {}

    def finding(level, area, message):
        findings.append({'level': level, 'area': area, 'message': message})

    for h in scope:
        try:
            warps = _warps(project, state, h)
        except Exception as exc:                                  # noqa: BLE001 (report, never block)
            finding('unanalyzed', h, f'Events could not be read: {exc}')
            continue
        areas.append({'header': h, 'name': entries[_ref('area', _area_key(project, h))]['name'], 'warps': len(warps),
                      'created': h in created})
        if h in created:
            try:
                dead[h] = _dead_entrances(project, state, h, warps)
            except Exception as exc:                              # noqa: BLE001
                finding('unanalyzed', h, f'Entrance tiles not checked: {exc}')
        for w in warps:
            edge = {'from': h, 'tile': [w['x'], w['z']], 'to': w['destination'], 'warp': w['destination_warp'],
                    'kind': 'warp', 'conditions': []}
            edges.append(edge)
            if not 0 <= w['destination'] < count:
                finding('error', h, f"Warp at {w['x']},{w['z']} leads to missing header {w['destination']}")
                continue
            try:
                target = _warps(project, state, w['destination'])
            except Exception:                                     # noqa: BLE001
                finding('unanalyzed', h, f"Warp at {w['x']},{w['z']}: destination {w['destination']} events unreadable")
                continue
            if w['destination_warp'] >= len(target) and w['destination_warp'] != 0xFF:
                finding('error', h, f"Warp at {w['x']},{w['z']} leads to warp {w['destination_warp']} of header "
                                    f"{w['destination']}, which has only {len(target)}")
    for key, s in sa.catalog(state, 'sequence').items():
        for n in s.get('nodes', []):
            if n.get('op') != 'warp':
                continue
            conditions = _conditions(s, n['id'])
            edges.append({'from': s['context']['header'], 'tile': [s['x'], s['z']], 'to': n['header'],
                          'kind': f'scripted travel ({key})', 'conditions': conditions})
            if conditions:
                finding('conditional', s['context']['header'],
                        f"Travel {key} → header {n['header']} needs: {'; '.join(conditions)}")
        if s.get('field'):
            gate = [c for n in s.get('nodes', []) if n.get('op') == 'require'
                    for c in [f"{n['kind']} {n.get(n['kind'], n.get('state', ''))}"]]
            finding('conditional', s['context']['header'],
                    f"Field object {key} ({s['field']['family']}) at {s['x']},{s['z']} opens after: "
                    + ('; '.join(gate) or 'using the move'))
    incoming = {e['to'] for e in edges if e['from'] != e['to']}
    for a in areas:
        if a['created'] and a['header'] not in incoming:
            finding('warning', a['header'], 'No entrance or travel leads here: the area is not connected to the world')
    for h, tiles in dead.items():                  # unconnected areas already carry the warning above
        if h in incoming or any(a['header'] == h and a['warps'] for a in areas):
            for x, z, kind in tiles:
                finding('warning', h, f'Entrance tile {x},{z} ({kind}) has no connection: stepping on it leads nowhere '
                                      '(connect it, or leave it as scenery)')
    if flood:
        for h in [a['header'] for a in areas if a['created'] and a['warps']]:
            try:
                from . import terrain_authoring as ta
                reach = ta.reachable(project, state, h, ledges=True)['foot']
            except Exception as exc:                              # noqa: BLE001
                finding('unanalyzed', h, f'Walkable reachability not determined: {exc}')
                continue
            for w in _warps(project, state, h):
                near = {(w['x'] + dx, w['z'] + dz) for dx, dz in ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1))}
                if not near & reach:
                    finding('warning', h, f"Entrance at {w['x']},{w['z']} is not reachable on foot from this area's "
                                          'other arrivals (check stairs, obstacles or Surf)')
    stock_scripted = sorted({h for h in scope if h not in created})
    for h in stock_scripted:
        finding('unanalyzed', h, 'Stock scripts and triggers in this area are not interpreted; only authored events '
                                 'and warps are analysed')
    levels = collections.Counter(f['level'] for f in findings)
    return {'revision': project.doc['revision'], 'areas': areas, 'edges': edges, 'findings': findings,
            'summary': dict(levels),
            'notes': ['error = definite problem; conditional = reachable only after the named requirement; '
                      'warning = static walk analysis could not reach it; unanalyzed = outside what the editor '
                      'interprets. Findings inform; they do not block export.']}


# ---- impact -------------------------------------------------------------------------------------

def impact(project, operations, label=None):
    """What a staged batch changes and who shares it (no write)."""
    plan = project.plan_area_edit(operations, label=label)
    trial = project.area_preview_project(plan)
    before, after = index(project), index(trial)
    changed = sorted(r for r in set(before) | set(after)
                     if before.get(r, {}).get('detail') != after.get(r, {}).get('detail')
                     or before.get(r, {}).get('uses') != after.get(r, {}).get('uses'))
    touched = set(changed)
    for t in plan['transactions']:
        ctx = t.get('context') or {}
        if ctx.get('header') is not None:
            touched.add(_ref('area', _area_key(project, ctx['header'])))
        for key in ('key',):
            if t.get('kind') in ('character', 'trainer', 'state') and t.get(key):
                touched.add(_ref(t['kind'], t[key]))
            if t.get('kind') == 'sequence' and t.get(key):
                touched.add(_ref('event', t[key]))
    rows = []
    for r in sorted(touched):
        e = after.get(r) or before.get(r)
        if not e:
            continue
        rows.append({'ref': r, 'kind': e['kind'], 'name': e['name'],
                     'change': 'added' if r not in before else 'removed' if r not in after else
                               'changed' if r in changed else 'context',
                     'shared_by': sorted(set(before.get(r, {}).get('used_by', [])) | set(e['used_by']))})
    return {'revision': project.doc['revision'], 'transactions': len(plan['transactions']), 'empty': plan['empty'],
            'label': plan['label'], 'preview': plan['preview'], 'affected': rows,
            'undo': 'one undoable edit; a stale revision is refused at apply'}


# ---- reach (ACCESS-01) --------------------------------------------------------------------------

ALTITUDE_TILES = 0.5            # 8 model units per matrix altitude step (tools/area_review.ALTITUDE_UNITS)


class _Matrix:
    """Lazily loaded composed tiles of one matrix (collision, behavior, height, objects, warps)."""

    def __init__(self, project, state, matrix_id, max_cells):
        self.project, self.state, self.id, self.max = project, state, matrix_id, max_cells
        self.mat = project.matrix_data(matrix_id)
        self.tiles, self.objects, self.warps, self.loaded = {}, {}, {}, set()

    def load(self, cx, cy, header_hint=None):
        from . import event_authoring as ev, terrain_geometry as tg
        from .terrain_authoring import bdhc_bytes
        if (cx, cy) in self.loaded or len(self.loaded) >= self.max:
            return False
        if not (0 <= cy < self.mat['height'] and 0 <= cx < self.mat['width']) or self.mat['maps'][cy][cx] == world.EMPTY:
            self.loaded.add((cx, cy))
            return False
        self.loaded.add((cx, cy))
        header = self.mat['headers'][cy][cx] if self.mat.get('headers') else header_hint
        try:
            ctx = self.project.context(header=header, cell=[cx, cy])
        except Exception:                                          # noqa: BLE001
            return False
        ox, oz = ctx['origin']
        raw = self.project.member_raw(ctx['map_member'])
        table = tg.parse_bdhc(bdhc_bytes(self.project, ctx['map_member']))
        alt = (self.mat['altitudes'][cy][cx] if self.mat.get('altitudes') else 0) * ALTITUDE_TILES
        for zz in range(oz, oz + 32):
            for xx in range(ox, ox + 32):
                off = world.cell_offset(ctx, xx, zz)
                pair = self.state['permissions'].get((ctx['map_member'], off), raw[off:off + 2])
                h = tg.height_at(table, xx - ox - 16 + 0.5, zz - oz - 16 + 0.5)
                self.tiles[(xx, zz)] = (pair[0], bool(pair[1] & 0x80), None if h is None else h + alt, header)
        for r in ev.records(ev.raw_member(self.project, ctx['event_member'], self.state)):
            if r['kind'] == 'npc':
                self.objects.setdefault((r['x'], r['z']), []).append(r)
            elif r['kind'] == 'warp' and ox <= r['x'] < ox + 32 and oz <= r['z'] < oz + 32:
                self.warps[(r['x'], r['z'])] = (header, r)
        return True

    def tile(self, t, header_hint=None):
        if t not in self.tiles:
            self.load(t[0] // 32, t[1] // 32, header_hint)
        return self.tiles.get(t)


def reach(project, header, x, z, flags=(), surf=False, conditional=True, max_cells=60, state=None):
    """Static walk/warp/travel reach from a tile, twice: authored field objects (Cut trees, Rock
    Smash rocks, Strength boulders, pickups) blocking, then opened. Events reached only in the
    second pass are gated by field moves. Other objects block unless their hide flag is in
    ``flags`` (e.g. the played save's). Not native evidence."""
    state = project.composed() if state is None else state
    closed = _reach(project, header, x, z, flags, surf, conditional, max_cells, state, open_fields=False)
    opened = _reach(project, header, x, z, flags, surf, conditional, max_cells, state, open_fields=True)
    after = {e['event']: e['reachable'] for e in opened['events']}
    fields = {k for k, s in sa.catalog(state, 'sequence').items() if s.get('field')}
    for e in closed['events']:
        e['after_field_moves'] = after.get(e['event'], False)
    closed['gated_by_field_moves'] = [e['event'] for e in closed['events'] if not e['reachable'] and e['after_field_moves']]
    closed['field_objects'] = sorted(fields)
    closed['reached_after_field_moves'] = sum(after.values())
    return closed


def _reach(project, header, x, z, flags, surf, conditional, max_cells, state, open_fields):
    from .terrain_authoring import SURFABLE
    from . import terrain_geometry as tg
    from collections import deque
    flags = set(flags)
    worlds, seen, todo, route, via = {}, set(), deque(), {}, {}

    def world_of(h):
        m = project.header(h)['matrix']
        if m not in worlds:
            worlds[m] = _Matrix(project, state, m, max_cells)
        return worlds[m]

    fields = {}
    if open_fields:
        for s_ in sa.catalog(state, 'sequence').values():
            if s_.get('field'):
                fields.setdefault(project.header(s_['context']['header'])['matrix'], set()).add((s_['x'], s_['z']))

    def blocked_by_object(w, t):
        if t in fields.get(w.id, ()):
            return False
        return any(not (o['flag'] and o['flag'] in flags) for o in w.objects.get(t, ()))

    def standable(w, t, h=None):
        v = w.tile(t, h)
        if v is None:
            return False
        behavior, blocked, height, _ = v
        return (not blocked and height is not None and (surf or behavior not in SURFABLE)
                and behavior not in tg.JUMP_DIRECTION and not blocked_by_object(w, t))

    def start(h, t, why):
        w = world_of(h)
        w.tile(t, h)
        key = (w.id, t)
        if key not in seen and w.tiles.get(t) is not None:
            seen.add(key)
            todo.append((w, t))
            route.setdefault((w.id, t), why)

    def drain():
        while todo:
            w, (tx, tz) = todo.popleft()
            here = w.tiles[(tx, tz)]
            for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                n = (tx + dx, tz + dz)
                v = w.tile(n, here[3])
                if v is None or (w.id, n) in seen:
                    continue
                if tg.JUMP_DIRECTION.get(v[0]) == (dx, dz):
                    land = (n[0] + dx, n[1] + dz)
                    if standable(w, land, here[3]) and (w.id, land) not in seen:
                        seen.add((w.id, land)); todo.append((w, land))
                    continue
                if standable(w, n, here[3]) and abs(v[2] - here[2]) < tg.BLOCK_DELTA:
                    seen.add((w.id, n)); todo.append((w, n))
            # Warps on or beside the tile (doors are entered from the tile below them).
            for t in ((tx, tz), (tx, tz - 1), (tx, tz + 1), (tx - 1, tz), (tx + 1, tz)):
                if t not in w.warps:
                    continue
                src, r = w.warps[t]
                key = ('warp', w.id, t)
                if key in via:
                    continue
                try:
                    dest = _warps(project, state, r['destination'])
                    arrival = dest[r['destination_warp']] if r['destination_warp'] < len(dest) else None
                except Exception:                                  # noqa: BLE001
                    arrival = None
                via[key] = {'from': [src, *t], 'to': r['destination'],
                            'arrival': [arrival['x'], arrival['z']] if arrival else None}
                if arrival:
                    start(r['destination'], (arrival['x'], arrival['z']), f"warp {src}:{t[0]},{t[1]}")
                    dw = world_of(r['destination'])
                    for ex, ez in ((0, 1), (0, -1), (1, 0), (-1, 0)):
                        e = (arrival['x'] + ex, arrival['z'] + ez)
                        if standable(dw, e, r['destination']):
                            start(r['destination'], e, f"warp {src}:{t[0]},{t[1]}")

    def reached_tiles():
        out = {}
        for (m, t) in seen:
            out.setdefault(m, set()).add(t)
        return out

    start(header, (x, z), 'start')
    drain()
    # Authored travel (talk-to-warp events) from reached tiles, until nothing new is reached.
    grown = True
    while grown:
        grown = False
        tiles_by_matrix = reached_tiles()
        for key, s in sa.catalog(state, 'sequence').items():
            h = s['context']['header']
            near = {(s['x'] + dx, s['z'] + dz) for dx, dz in ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1))}
            if not near & tiles_by_matrix.get(project.header(h)['matrix'], set()):
                continue
            for n in s.get('nodes', []):
                if n.get('op') != 'warp' or ('travel', key, n['id']) in via:
                    continue
                conditions = _conditions(s, n['id'])
                if conditions and not conditional:
                    continue
                via[('travel', key, n['id'])] = {'from': [h, s['x'], s['z']], 'to': n['header'],
                                                 'arrival': [n['x'], n['z']], 'conditions': conditions}
                before = len(seen)
                start(n['header'], (n['x'], n['z']), f'travel {key}')
                drain()
                grown = grown or len(seen) > before
    reached_tiles = reached_tiles()
    events = []
    for key, s in sa.catalog(state, 'sequence').items():
        mid = project.header(s['context']['header'])['matrix']
        near = {(s['x'] + dx, s['z'] + dz) for dx, dz in ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1))}
        events.append({'event': key, 'header': s['context']['header'], 'tile': [s['x'], s['z']],
                       'reachable': bool(near & reached_tiles.get(mid, set()))})
    by_header = collections.Counter(w.tiles[t][3] for (m, t) in seen for w in [next(v for v in worlds.values() if v.id == m)])
    return {'revision': project.doc['revision'], 'start': {'header': header, 'x': x, 'z': z},
            'tiles': len(seen), 'by_header': {str(k): v for k, v in sorted(by_header.items(), key=lambda kv: str(kv[0]))},
            'links': [dict(kind=k[0], **v) for k, v in via.items()],
            'events': events, 'reached': sum(e['reachable'] for e in events),
            'rules': 'collision bit; one-way ledges; |dh| < 1.25 tile; water only with surf; map objects block '
                     'unless their hide flag is set; warps and authored travel followed; static, not native evidence'}
