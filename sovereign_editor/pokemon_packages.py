"""Custom Pokémon art packages and persistent form identities (POKE-01..04).

Qualified against the pinned ROM and this build's own hg-engine source (src/pokemon.c,
src/field/overworld_table.c, armips/data/*.s; pret src/follow_mon.c; ledger
work/original-content-v1/impl/LEDGER.md):

* An identity is a new persistent FORM of an existing base species with its own personal index
  P (the first index after every stock/engine record, 1490 in the pinned build). The form number
  is saved in each Pokémon (5 bits), so saves keep it. hg-engine resolves (species, form) -> P
  through the form table (a/0/2/8 member 11, row species, entry form-1) for personal data
  (a/0/0/2 P), level-up row P (a/0/3/3 member 0), evolutions (a/0/3/4 P), battle/summary pictures
  (a/0/0/4 6P..6P+5: female back, male back, female front, male front, normal, shiny), picture
  heights (a/0/0/5 4P..4P+3), battle sprite offsets/animations (a/1/8/0 member 0, 0x59 B row P),
  party/PC icon (a/0/2/0 7+P) and its palette selector (gIconPalTable[P], overlay 129), TM and
  tutor compatibility (a/0/2/8 members 14/15), base experience and hidden ability (members 8/7,
  indexed by P) and P -> base species (member 12, index P - 1076). The first free form slot of
  the base species is allocated (Eevee: form 1 = partner, form 2 = a battle-only form, so 3).
* The NARC reader has no member-end check, so the engine's custom megas (1476..1489) currently read
  base experience / hidden ability from the bytes that follow those lists. The lists are extended
  with exactly those bytes before P's own value, so every existing index reads what it read before.
  Evolution records stop at 1473 (the megas never evolve); 1474..P-1 become empty records.
* gIconPalTable (1490 bytes at 0x023D8010, followed by other overlay-129 data) moves to the boot
  data region with the new selectors; its two references (ARM9 0x02074408, overlay 129 0x023DA864)
  are repointed.
* Followers: get_mon_ow_tag (overlay 129) is reached through two hook literals (ARM9 0x02069D74,
  the FollowMon_GetSpriteID hook; overlay 131 0x023C8F64). Both now name a resident wrapper that
  returns the identity's own tag (0x2400 + k) and otherwise tail-calls the original with its
  arguments and return address intact. The tag -> a/0/8/1 table gains one row per identity (the
  table is copied once more to the end of overlay 131; overlay 1 reads it through the same two
  repointed literals as character overworlds). Size/shadow parameters (a/1/4/1) stay the base
  species' own, since the stock form count keeps the base offset.
* Inherited, not customized: species name, Pokédex entry, cry and footprint are the base
  species'. Personal data, learnset, TM/tutor sets, base experience and hidden ability are copied
  from the base species at export unless the identity names its own; evolutions default to none.

Pure candidate bytes and validation; Project owns writes.
"""
import copy
import json
import re
import struct
from pathlib import Path

from .formats import EditorError, digest, member_count, require, resource

SCHEMA = 'sovereign-pokemon-transaction-v1'
ACTIONS = ('import', 'revise', 'bind', 'remove')
KEY = re.compile(r'[a-z][a-z0-9_]{0,23}')
PERSONAL, PICTURES, HEIGHTS, ICONS = 'a/0/0/2', 'a/0/0/4', 'a/0/0/5', 'a/0/2/0'
LEVELUP, EVOLUTION, OFFSETS, ADDONS, OVERWORLD = 'a/0/3/3', 'a/0/3/4', 'a/1/8/0', 'a/0/2/8', 'a/0/8/1'
STOCK_PERSONAL = 1490                      # first free personal index in the pinned build
MEGA_START = 1076                          # member 12 index base (SPECIES_MEGA_START)
MAX_MON_NUM = 1075
HIDDEN_ABILITY, BASE_EXP, FORM_TABLE, FORM_SPECIES, MACHINES, TUTORS = 7, 8, 11, 12, 14, 15
ROW = {LEVELUP: 136, OFFSETS: 0x59, MACHINES: 44, TUTORS: 8}
EVOLUTION_SIZE, EVOLUTION_STOCK = 56, 1474
LIST_ENTRIES = 1476                        # members 7/8: u16 per index 0..1475
NEEDS_REVERSION = 0x8000
MAX_IDENTITIES = 16
LIMBO = range(494, 544)

ICON_TABLE, ICON_COUNT = 0x023D8010, STOCK_PERSONAL
ICON_REFS = ((None, 0x02074408), (129, 0x023DA864))
OW_TAG_FN = 0x023DB894                     # get_mon_ow_tag (overlay 129)
OW_TAG_REFS = ((None, 0x02069D74), (131, 0x023C8F64))
OW_TABLE_REFS = ((1, 0x021F92FC), (1, 0x021FA280), (131, 0x023C8F1C))
STOCK_OW_TABLE, FIRST_TAG = 0x023CA260, 0x2400
OW_SMALL = 0x4E27
STOCK_OVERWORLDS = 1553

ROLES = {'front': 'native/front.NCGR', 'back': 'native/back.NCGR', 'normal': 'native/normal.NCLR',
         'shiny': 'native/shiny.NCLR', 'icon': 'native/icon.NCGR', 'follower': 'native/follower.btx0'}
OPTIONAL = {'female_front': 'native/female_front.NCGR', 'female_back': 'native/female_back.NCGR'}
SOURCE_SUFFIXES = ('.aseprite', '.png', '.pal', '.json', '.md', '.txt')
SOURCE_LIMIT = 4 << 20


def is_transaction(t):
    return isinstance(t, dict) and t.get('schema') == SCHEMA


def packages(state):
    return (state.get('pokemon') or {}).get('packages', {})


def identities(state):
    return (state.get('pokemon') or {}).get('identities', {})


# ---- template records -------------------------------------------------------------------------

def _word(raw, at):
    return struct.unpack_from('<H', raw, at)[0]


def form_entry(blob, species, form):
    return _word(resource(blob, ADDONS, FORM_TABLE)[1], 2 * (32 * species + form - 1))


def base_tag(blob, species):
    ow = _word(resource(blob, ADDONS, 10)[1], 2 * species)
    return ow + (0x1E4 if species > 456 else 0x1AC)


def tag_rows(blob, address=STOCK_OW_TABLE, data=None):
    from . import character_runtime as cr
    if data is None:
        data = cr.overlay(blob, 131)['data']
    at, rows = address - 0x023C8000, []
    while True:
        row = struct.unpack_from('<3H', data, at + 6 * len(rows))
        if row[0] == 0xFFFF:
            return rows
        rows.append(row)


def template_records(blob, species):
    """The base species' own native members an imported package must match."""
    pics = [resource(blob, PICTURES, 6 * species + k)[1] for k in range(6)]
    gfx = next(g for t, g, _ in tag_rows(blob) if t == base_tag(blob, species))
    return {'back': pics[1], 'front': pics[3], 'normal': pics[4], 'shiny': pics[5],
            'icon': resource(blob, ICONS, 7 + species)[1], 'follower': resource(blob, OVERWORLD, gfx)[1],
            'female_front': pics[2], 'female_back': pics[0]}


def _tex0_shape(raw):
    """A BTX0 with its texel and palette data zeroed: names, formats and sizes only."""
    at = struct.unpack_from('<I', raw, 0x10)[0]
    shape = bytearray(raw)
    tex_size, tex_off = _word(raw, at + 0x0C) << 3, struct.unpack_from('<I', raw, at + 0x14)[0]
    pal_size, pal_off = _word(raw, at + 0x30) << 3, struct.unpack_from('<I', raw, at + 0x38)[0]
    for off, size in ((tex_off, tex_size), (pal_off, pal_size)):
        shape[at + off:at + off + size] = bytes(size)
    return bytes(shape)


def check_role(role, raw, template):
    """Same container, size and header as the template species' record (content may differ)."""
    require(len(raw) == len(template), f'{role}: {len(raw)} bytes, the template record has {len(template)}',
            'INVALID_ASSET')
    if role == 'follower':
        require(raw[:4] == b'BTX0' and _tex0_shape(raw) == _tex0_shape(template),
                'follower: texture/palette layout differs from the template follower (8 textures, 2 palettes)',
                'INVALID_ASSET')
    elif role in ('normal', 'shiny'):
        require(raw[:40] == template[:40], f'{role}: palette header differs (16 colours expected)', 'INVALID_ASSET')
    else:
        require(raw[:48] == template[:48], f'{role}: character header differs from the template record',
                'INVALID_ASSET')


# ---- packages -----------------------------------------------------------------------------------

def load_source(folder):
    folder = Path(folder)
    require(folder.is_dir(), 'Choose a prepared Pokémon package folder', 'NOT_FOUND')
    roles = {}
    for role, rel in {**ROLES, **OPTIONAL}.items():
        path = folder / rel
        if path.is_file():
            roles[role] = rel
        else:
            require(role in OPTIONAL, f'{rel} is missing', 'NOT_FOUND')
    files, total = {}, 0
    for path in sorted(folder.rglob('*')):
        if not path.is_file() or path.name.startswith('.'):
            continue
        rel = path.relative_to(folder).as_posix()
        keep = rel in roles.values() or (path.suffix.lower() in SOURCE_SUFFIXES
                                          and not path.name.startswith(('preview-', 'comparison-')))
        if keep:
            files[rel] = path.read_bytes()
            total += len(files[rel])
    require(total <= SOURCE_LIMIT, 'Package sources exceed 4 MiB; keep previews outside the package', 'RESOURCE_CAPACITY')
    return {'roles': roles, 'files': files}


def package(blob, source, key, display, template, icon_palette):
    require(isinstance(key, str) and KEY.fullmatch(key), 'Package id: lowercase letters/digits/_ (1..24)',
            'INVALID_INPUT')
    require(isinstance(display, str) and 0 < len(display) <= 40, 'Display name: 1..40 characters', 'INVALID_INPUT')
    require(type(template) is int and 0 < template <= MAX_MON_NUM and template not in LIMBO,
            'Template: a base species (its records set the native layout)', 'INVALID_INPUT')
    require(icon_palette in (0, 1, 2), 'Icon palette selector is 0, 1 or 2', 'INVALID_INPUT')
    templates = template_records(blob, template)
    for role, rel in source['roles'].items():
        check_role(role, source['files'][rel], templates[role])
    stored = {'source/' + rel: data for rel, data in source['files'].items()}
    body = {'id': key, 'display': display, 'template': template, 'icon_palette': icon_palette,
            'roles': {role: 'source/' + rel for role, rel in source['roles'].items()},
            'files': {rel: digest(data) for rel, data in stored.items()},
            'report': {'roles': sorted(source['roles']), 'sources': len(source['files']),
                       'bytes': sum(len(d) for d in source['files'].values()),
                       'female_art': 'own' if 'female_front' in source['roles'] else 'same as male'}}
    sha = digest(json.dumps(body, sort_keys=True).encode())
    return sha, {**stored, 'manifest.json': json.dumps(body, sort_keys=True, indent=1).encode()}, body


def package_dir(project, key, sha):
    return project.root / 'assets' / 'pokemon' / key / sha[:16]


def pending(project):
    return project.__dict__.setdefault('_pokemon_pending', {})


def write_package(project, key, sha, files):
    target = package_dir(project, key, sha)
    if target.is_dir():
        verify_package(project, key, sha)
        return target
    temp = target.with_name(target.name + '.partial')
    import shutil
    shutil.rmtree(temp, ignore_errors=True)
    for rel, data in files.items():
        path = temp / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    temp.rename(target)
    return target


def verify_package(project, key, sha):
    staged = pending(project).get((key, sha))
    if staged is not None:
        return staged['body']
    cache = project.__dict__.setdefault('_pokemon_packages', {})
    if (key, sha) in cache:
        return cache[(key, sha)]
    folder = package_dir(project, key, sha)
    require((folder / 'manifest.json').is_file(), f'Pokémon package {key}/{sha[:16]} is missing from the project',
            'MISSING_ASSET')
    body = json.loads((folder / 'manifest.json').read_text())
    require(digest(json.dumps(body, sort_keys=True).encode()) == sha, f'Pokémon package {key} manifest differs',
            'BEFORE_VALUE_MISMATCH')
    for rel, want in body['files'].items():
        path = folder / rel
        require(path.is_file() and digest(path.read_bytes()) == want,
                f'Pokémon package file {key}/{rel} differs or is missing', 'BEFORE_VALUE_MISMATCH')
    cache[(key, sha)] = body
    return body


def role_bytes(project, key, sha, role):
    staged = pending(project).get((key, sha))
    body = verify_package(project, key, sha)
    rel = body['roles'].get(role) or body['roles'][role.replace('female_', '')]
    if staged is not None:
        return staged['files'][rel]
    return (package_dir(project, key, sha) / rel).read_bytes()


def stage(project, folder, key, display, template, icon_palette):
    """Validate a package folder in memory (no writes) and return the operation to apply."""
    source = load_source(folder)
    sha, files, body = package(project.blob, source, key, display, template, icon_palette)
    pending(project)[(key, sha)] = {'files': files, 'body': body}
    current = packages(project.composed()).get(key)
    op = ({'action': 'revise', 'key': key, 'package': sha, 'expected_revision': current['revision']} if current
          else {'action': 'import', 'key': key, 'package': sha})
    return {'key': key, 'package': sha, 'report': body['report'], 'unchanged': bool(current and current['package'] == sha),
            'users': sorted(k for k, i in identities(project.composed()).items() if i['package'] == key),
            'operation': op}


# ---- identities ---------------------------------------------------------------------------------

def free_form(blob, state, species):
    """First empty form slot of a base species (occupied rows and existing identities excluded)."""
    taken = {i['form'] for i in identities(state).values() if i['species'] == species}
    for form in range(1, 32):
        if not form_entry(blob, species, form) and form not in taken:
            return form
    require(False, f'Species {species} has no free form slot', 'RESOURCE_CAPACITY')


def plan(project, state, index, action, key=None, label=None, **fields):
    require(action in ACTIONS, f"Pokémon action is one of {', '.join(ACTIONS)}", 'INVALID_INPUT')
    require(isinstance(key, str) and KEY.fullmatch(key), 'Name the package or identity key', 'INVALID_INPUT')
    require(label is None or isinstance(label, str) and 0 < len(label) <= 160, 'Invalid label', 'INVALID_INPUT')
    table, ids = packages(state), identities(state)
    before, after, report = None, None, None
    if action in ('import', 'revise'):
        require(set(fields) <= {'package', 'expected_revision'}, 'Import/revise take package and expected_revision',
                'INVALID_INPUT')
        sha = fields.get('package')
        require(isinstance(sha, str) and len(sha) == 64, 'Stage the package folder first', 'INVALID_INPUT')
        body = verify_package(project, key, sha)
        before = copy.deepcopy(table.get(key))
        if action == 'import':
            require(before is None, f'Package {key} exists; revise it', 'EXISTS')
            after = {'package': sha, 'revision': 1, 'display': body['display'], 'template': body['template']}
        else:
            require(before is not None, f'No package {key}', 'NOT_FOUND')
            require(fields.get('expected_revision') == before['revision'], 'The package changed; reload',
                    'STALE_EDIT')
            require(sha != before['package'], 'The package is unchanged', 'NO_CHANGE')
            require(body['template'] == before['template'], 'A revision keeps the template species',
                    'INVALID_INPUT')
            after = {'package': sha, 'revision': before['revision'] + 1, 'display': body['display'],
                     'template': body['template']}
        report = {**body['report'], 'identities': sorted(k for k, i in ids.items() if i['package'] == key)}
    elif action == 'bind':
        require(set(fields) <= {'package', 'species', 'evolution'}, 'Bind takes package, species and evolution',
                'INVALID_INPUT')
        require(key not in ids, f'Identity {key} exists (identities are permanent)', 'EXISTS')
        require(len(ids) < MAX_IDENTITIES, f'At most {MAX_IDENTITIES} custom identities', 'RESOURCE_CAPACITY')
        pkg = table.get(fields.get('package'))
        require(pkg is not None, 'Import the package before binding it', 'NOT_FOUND')
        species = fields.get('species', pkg['template'])
        require(species == pkg['template'], 'The identity is a form of its package template species', 'INVALID_INPUT')
        evolution = fields.get('evolution', 'none')
        require(evolution in ('none', 'inherit'), "Evolution is 'none' or 'inherit'", 'INVALID_INPUT')
        form = free_form(project.blob, state, species)
        personal = STOCK_PERSONAL + len(ids)
        after = {'package': fields['package'], 'species': species, 'form': form, 'personal': personal,
                 'tag': FIRST_TAG + len(ids), 'data': 'inherit', 'evolution': evolution}
        report = {'form': form, 'personal': personal, 'inherits': ['stats/types/abilities', 'learnset', 'TM/tutor',
                  'base experience', 'hidden ability', 'picture heights/offsets', 'follower size', 'cry', 'footprint',
                  'species name/Pokédex'], 'evolution': evolution}
    else:
        require(not fields, 'Remove takes only the key', 'INVALID_INPUT')
        before = copy.deepcopy(table.get(key))
        require(before is not None, f'No package {key}', 'NOT_FOUND')
        users = sorted(k for k, i in ids.items() if i['package'] == key)
        require(not users, f"Identities use this package: {', '.join(users)}", 'IN_USE')
    return {'schema': SCHEMA, 'index': index, 'action': action, 'key': key,
            'label': label or f'Pokémon {action} {key}', 'request': {'action': action, 'key': key, 'label': label,
                                                                    **copy.deepcopy(fields)},
            'before': before, 'after': after, 'report': report}


def replay(project, state, t, index):
    try:
        expected = plan(project, state, index, **t['request'])
    except (KeyError, TypeError) as exc:
        raise EditorError('INVALID_INPUT', 'Malformed Pokémon transaction') from exc
    require(expected == t, 'Pokémon package before-value or allocation differs', 'BEFORE_VALUE_MISMATCH')
    root = state.setdefault('pokemon', {})
    if t['action'] == 'bind':
        root.setdefault('identities', {})[t['key']] = copy.deepcopy(t['after'])
    elif t['action'] == 'remove':
        root.setdefault('packages', {}).pop(t['key'], None)
    else:
        root.setdefault('packages', {})[t['key']] = copy.deepcopy(t['after'])


def summary(t):
    return {'operation': 'pokemon.transaction', 'index': t['index'], 'action': t['action'], 'key': t['key'],
            'label': t['label'], 'before': t['before'], 'after': t['after'], 'report': t['report']}


def lookup(state, species, form):
    return next((dict(i, key=k) for k, i in identities(state).items()
                 if i['species'] == species and i['form'] == form), None)


def view(project, state):
    rows = []
    for key, p in sorted(packages(state).items()):
        body = verify_package(project, key, p['package'])
        rows.append({'key': key, **p, 'roles': sorted(body['roles']), 'icon_palette': body['icon_palette'],
                     'identities': sorted(k for k, i in identities(state).items() if i['package'] == key)})
    return {'packages': rows,
            'identities': [{'key': k, **i} for k, i in sorted(identities(state).items(), key=lambda kv: kv[1]['personal'])],
            'capacity': {'identities': [len(identities(state)), MAX_IDENTITIES]},
            'inherited': 'species name, Pokédex, cry and footprint are the base species\''}


# ---- runtime ------------------------------------------------------------------------------------

def qualify_runtime(blob):
    from . import character_runtime as cr
    counts = {PERSONAL: 1490, PICTURES: 8940, HEIGHTS: 5960, ICONS: 1497, EVOLUTION: EVOLUTION_STOCK}
    for archive, want in counts.items():
        require(member_count(blob, archive) == want, f'{archive} member count differs from the qualified build',
                'UNSUPPORTED_RUNTIME')
    require(len(resource(blob, LEVELUP, 0)[1]) == 1490 * ROW[LEVELUP] and
            len(resource(blob, OFFSETS, 0)[1]) == 1490 * ROW[OFFSETS], 'Row tables differ', 'UNSUPPORTED_RUNTIME')
    for member, size in ((HIDDEN_ABILITY, 2 * LIST_ENTRIES), (BASE_EXP, 2 * LIST_ENTRIES),
                         (FORM_SPECIES, 2 * (STOCK_PERSONAL - MEGA_START)), (MACHINES, 1490 * ROW[MACHINES]),
                         (TUTORS, 1490 * ROW[TUTORS]), (FORM_TABLE, 64 * MAX_MON_NUM)):
        require(len(resource(blob, ADDONS, member)[1]) == size, f'a/0/2/8 member {member} differs',
                'UNSUPPORTED_RUNTIME')
    arm_start = struct.unpack_from('<I', blob, 0x20)[0]
    for ov, address in ICON_REFS:
        data, base = (blob, arm_start - 0x02000000) if ov is None else (cr.overlay(blob, ov)['data'], -cr.overlay(blob, ov)['address'])
        require(struct.unpack_from('<I', data, address + base)[0] == ICON_TABLE, 'Icon palette reference differs',
                'UNSUPPORTED_RUNTIME')
    for ov, address in OW_TAG_REFS:
        data, base = (blob, arm_start - 0x02000000) if ov is None else (cr.overlay(blob, ov)['data'], -cr.overlay(blob, ov)['address'])
        require(struct.unpack_from('<I', data, address + base)[0] == OW_TAG_FN | 1, 'Follower tag hook differs',
                'UNSUPPORTED_RUNTIME')
    require(member_count(blob, OVERWORLD) == STOCK_OVERWORLDS, 'a/0/8/1 member count differs', 'UNSUPPORTED_RUNTIME')
    return True


def _member(project, state, archive, member):
    from .gameplay import current
    return current(project, state, archive, member)


def records(project, state, identity):
    """The appended native records of one identity (inherited data read from the current base)."""
    blob, s, p = project.blob, identity['species'], identity['package']
    sha = packages(state)[p]['package']
    body = verify_package(project, p, sha)
    art = {role: role_bytes(project, p, sha, role) for role in ('front', 'back', 'normal', 'shiny', 'icon', 'follower')}
    female_front = role_bytes(project, p, sha, 'female_front') if 'female_front' in body['roles'] else art['front']
    female_back = role_bytes(project, p, sha, 'female_back') if 'female_back' in body['roles'] else art['back']
    evolution = (bytes(EVOLUTION_SIZE) if identity['evolution'] == 'none'
                 else _member(project, state, EVOLUTION, s))
    return {'personal': _member(project, state, PERSONAL, s),
            'pictures': [female_back, art['back'], female_front, art['front'], art['normal'], art['shiny']],
            'heights': [resource(blob, HEIGHTS, 4 * s + k)[1] for k in range(4)],
            'icon': art['icon'], 'icon_palette': body['icon_palette'], 'follower': art['follower'],
            'evolution': evolution,
            'learnset': _member(project, state, LEVELUP, 0)[s * ROW[LEVELUP]:(s + 1) * ROW[LEVELUP]],
            'offsets': resource(blob, OFFSETS, 0)[1][s * ROW[OFFSETS]:(s + 1) * ROW[OFFSETS]],
            'machines': _member(project, state, ADDONS, MACHINES)[s * ROW[MACHINES]:(s + 1) * ROW[MACHINES]],
            'tutors': _member(project, state, ADDONS, TUTORS)[s * ROW[TUTORS]:(s + 1) * ROW[TUTORS]],
            'base_exp': resource(blob, ADDONS, BASE_EXP)[1][2 * s:2 * s + 2],
            'hidden_ability': resource(blob, ADDONS, HIDDEN_ABILITY)[1][2 * s:2 * s + 2]}


def tag_wrapper(address, table, count):
    """Replaces get_mon_ow_tag at its two hook literals: r0 species, r1 form, r2 isFemale."""
    from .resident import thumb
    return thumb([
        0xB430,                  # push {r4, r5}
        ('ldr', 3, table),
        0x2400 | count,          # movs r4, #count
        'loop',
        0x2C00, ('b', 0, 'stock'),           # cmp r4, #0; beq stock
        0x881D, 0x4285, ('b', 1, 'next'),    # ldrh r5, [r3]; cmp r5, r0; bne next
        0x885D, 0x428D, ('b', 1, 'next'),    # ldrh r5, [r3, #2]; cmp r5, r1; bne next
        0x8898,                  # ldrh r0, [r3, #4]      the identity's tag
        0xBC30, 0x4770,          # pop {r4, r5}; bx lr
        'next',
        0x3308, 0x3C01, ('b', None, 'loop'), # adds r3, #8; subs r4, #1
        'stock',
        0xBC30,                  # pop {r4, r5}
        ('ldr', 3, OW_TAG_FN | 1),
        0x4718,                  # bx r3 (arguments and lr untouched)
    ], address)


def _patch_word(plan, blob, ov, address, before, after, kind):
    from . import character_runtime as cr
    if ov is None:
        start = struct.unpack_from('<I', blob, 0x20)[0]
        offset = start + address - 0x02000000
        existing = next((p for p in plan['patches'] if p['rom_offset'] == offset), None)
        if existing:
            require(bytes.fromhex(existing['after']) == struct.pack('<I', before), f'{kind}: conflicting ARM9 patch',
                    'RESOURCE_CONFLICT')
            existing['after'] = struct.pack('<I', after).hex()
        else:
            require(blob[offset:offset + 4] == struct.pack('<I', before), f'{kind}: before-value differs',
                    'BEFORE_VALUE_MISMATCH')
            plan['patches'].append({'rom_offset': offset, 'before': struct.pack('<I', before).hex(),
                                    'after': struct.pack('<I', after).hex(), 'kind': kind})
        return
    info = cr.overlay(blob, ov) if ov != 1 else _overlay1(blob)
    data = bytearray(plan['files'].get(info['file_id'], info['data']))
    at = address - info['address']
    require(bytes(data[at:at + 4]) == struct.pack('<I', before), f'{kind}: overlay {ov} before-value differs',
            'BEFORE_VALUE_MISMATCH')
    data[at:at + 4] = struct.pack('<I', after)
    plan['files'][info['file_id']] = bytes(data)


def _overlay1(blob):
    from . import character_runtime as cr
    table, size = struct.unpack_from('<II', blob, 0x50)
    off = next(o for o in range(table, table + size, 32) if struct.unpack_from('<I', blob, o)[0] == 1)
    entry = struct.unpack_from('<8I', blob, off)
    return {'address': entry[1], 'file_id': entry[6], 'data': cr.file_by_id(blob, entry[6])[1]}


def _extend_list(raw, follower, entries, values):
    """u16 list of ``entries`` extended to index len(values)-1 onward: indexes past the old end
    first get exactly the bytes the engine read there before (the following member's head)."""
    out = bytearray(raw)
    need = 2 * (STOCK_PERSONAL - entries)
    out += bytes(follower[:need])
    for v in values:
        out += v
    return bytes(out)


def bindings(project, state, plan, layout):
    ids = sorted(identities(state).items(), key=lambda kv: kv[1]['personal'])
    if not ids:
        return plan
    from . import character_runtime as cr
    blob = project.blob
    qualify_runtime(blob)
    require([i['personal'] for _, i in ids] == list(range(STOCK_PERSONAL, STOCK_PERSONAL + len(ids))),
            'Custom personal indexes are not dense', 'UNSUPPORTED_RUNTIME')
    result = {**plan, 'files': dict(plan['files']), 'patches': [dict(p) for p in plan['patches']],
              'appends': dict(plan.get('appends', {})), 'replacements': dict(plan.get('replacements', {}))}
    recs = [(k, i, records(project, state, i)) for k, i in ids]
    appends = result['appends']
    for archive in (PERSONAL, PICTURES, HEIGHTS, ICONS, EVOLUTION):
        require(archive not in appends, f'Another runtime edit appends to {archive}', 'RESOURCE_CONFLICT')
    appends[PERSONAL] = [r['personal'] for _, _, r in recs]
    appends[PICTURES] = [pic for _, _, r in recs for pic in r['pictures']]
    appends[HEIGHTS] = [h for _, _, r in recs for h in r['heights']]
    appends[ICONS] = [r['icon'] for _, _, r in recs]
    appends[EVOLUTION] = [bytes(EVOLUTION_SIZE)] * (STOCK_PERSONAL - EVOLUTION_STOCK) + [r['evolution'] for _, _, r in recs]
    # Row tables and a/0/2/8 lists grow by one row per identity.
    reps = {}
    reps[(LEVELUP, 0)] = _member(project, state, LEVELUP, 0) + b''.join(r['learnset'] for _, _, r in recs)
    reps[(OFFSETS, 0)] = resource(blob, OFFSETS, 0)[1] + b''.join(r['offsets'] for _, _, r in recs)
    reps[(ADDONS, MACHINES)] = _member(project, state, ADDONS, MACHINES) + b''.join(r['machines'] for _, _, r in recs)
    reps[(ADDONS, TUTORS)] = _member(project, state, ADDONS, TUTORS) + b''.join(r['tutors'] for _, _, r in recs)
    reps[(ADDONS, FORM_SPECIES)] = resource(blob, ADDONS, FORM_SPECIES)[1] + b''.join(
        struct.pack('<H', i['species']) for _, i, _ in recs)
    reps[(ADDONS, HIDDEN_ABILITY)] = _extend_list(resource(blob, ADDONS, HIDDEN_ABILITY)[1],
                                                  resource(blob, ADDONS, HIDDEN_ABILITY + 1)[1], LIST_ENTRIES,
                                                  [r['hidden_ability'] for _, _, r in recs])
    reps[(ADDONS, BASE_EXP)] = _extend_list(resource(blob, ADDONS, BASE_EXP)[1], resource(blob, ADDONS, BASE_EXP + 1)[1],
                                            LIST_ENTRIES, [r['base_exp'] for _, _, r in recs])
    forms = bytearray(resource(blob, ADDONS, FORM_TABLE)[1])
    for _, i, _ in recs:
        at = 2 * (32 * i['species'] + i['form'] - 1)
        require(_word(forms, at) == 0, 'The allocated form slot is occupied', 'BEFORE_VALUE_MISMATCH')
        struct.pack_into('<H', forms, at, i['personal'])
    reps[(ADDONS, FORM_TABLE)] = bytes(forms)
    for (archive, member), payload in reps.items():
        target = result['replacements'].setdefault(archive, {})
        require(member not in target, f'Another runtime edit replaces {archive} {member}', 'RESOURCE_CONFLICT')
        target[member] = payload
    # Icon palette selectors: the stock table plus one byte per identity, in the boot region.
    ov129 = cr.overlay(blob, 129)
    stock = bytes(ov129['data'][ICON_TABLE - ov129['address']:ICON_TABLE - ov129['address'] + ICON_COUNT])
    icon_table = layout.place_boot('pokemon.icon-palettes', ICON_COUNT + len(recs), 4,
                                   note=f'gIconPalTable: {ICON_COUNT} stock selectors + {len(recs)} custom identities')
    layout.write(icon_table, stock + bytes(r['icon_palette'] for _, _, r in recs))
    for ov, address in ICON_REFS:
        if ov == 129:
            layout.patch(address, struct.pack('<I', ICON_TABLE), struct.pack('<I', icon_table))
        else:
            _patch_word(result, blob, ov, address, ICON_TABLE, icon_table, 'pokemon.icon-palette-table')
    # Followers: graphics after any character overworlds; one tag row each.
    gfx0 = STOCK_OVERWORLDS + len(appends.get(OVERWORLD, []))
    appends[OVERWORLD] = list(appends.get(OVERWORLD, [])) + [r['follower'] for _, _, r in recs]
    tags = b''.join(struct.pack('<HHHH', i['species'], i['form'], i['tag'], 0) for _, i, _ in recs)
    tag_table = layout.place_boot('pokemon.follower-tags', len(tags), 4, note='(species, form, tag) per identity')
    layout.write(tag_table, tags)
    wrapper = layout.place('pokemon.follower-tag', len(tag_wrapper(0, 0, len(recs))), 4, 'code',
                           note='get_mon_ow_tag wrapper at its two hook literals (identity tags, else stock)')
    layout.write(wrapper, tag_wrapper(wrapper, tag_table, len(recs)))
    for ov, address in OW_TAG_REFS:
        _patch_word(result, blob, ov, address, OW_TAG_FN | 1, wrapper | 1, 'pokemon.follower-tag-hook')
    info131 = cr.overlay(blob, 131)
    data131 = bytearray(result['files'].get(info131['file_id'], info131['data']))
    current = struct.unpack_from('<I', data131, 0x023C8F1C - info131['address'])[0]
    rows = tag_rows(blob, current, data131)
    table_bytes = (b''.join(struct.pack('<3H', *r) for r in rows)
                   + b''.join(struct.pack('<3H', i['tag'], gfx0 + n, OW_SMALL) for n, (_, i, _) in enumerate(recs))
                   + struct.pack('<3H', 0xFFFF, 0, 0))
    data131.extend(b'\0' * (-len(data131) % 4))
    new_table = info131['address'] + len(data131)
    data131.extend(table_bytes)
    require(len(data131) <= cr.FIELD_LIMIT, 'Follower table exceeds the field extension reservation',
            'RESOURCE_CAPACITY')
    result['files'][info131['file_id']] = bytes(data131)
    for ov, address in OW_TABLE_REFS:
        _patch_word(result, blob, ov, address, current, new_table, 'pokemon.follower-table')
    offset = info131['table_offset'] + 8
    existing = next((p for p in result['patches'] if p['rom_offset'] == offset), None)
    if existing:
        existing['after'] = struct.pack('<I', len(data131)).hex()
    else:
        result['patches'].append({'rom_offset': offset, 'before': blob[offset:offset + 4].hex(),
                                  'after': struct.pack('<I', len(data131)).hex(), 'kind': 'pokemon.overlay-size'})
    result['pokemon'] = {'identities': [{'key': k, 'species': i['species'], 'form': i['form'],
                                         'personal': i['personal'], 'tag': i['tag'], 'follower_gfx': gfx0 + n}
                                        for n, (k, i, _) in enumerate(recs)],
                         'icon_table': icon_table, 'tag_table': tag_table, 'wrapper': wrapper,
                         'follower_table': new_table}
    return result
