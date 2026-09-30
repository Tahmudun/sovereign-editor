"""Qualified expanded species and regional forms of the pinned Sovereign Gold build.

Read-only. Every row is derived from THIS ROM's own data, never from a generic
hg-engine or stock-HGSS claim (docs/ASSETS_GAMEPLAY_COVERAGE.md has the evidence):

* ``a/0/2/8`` member 11 (CODE_ADDON_FORM_DATA): u16[1075][32] (species 0..1074); entry ``f-1`` of a
  base species is the personal index of form ``f`` (bit 15 = battle-only form that
  needs reversion). ``PokeOtherFormMonsNoGet`` (ov129 0x023DA451) reads it.
* A personal index ``p`` addresses personal ``a/0/0/2`` p, learnset row p of
  ``a/0/3/3`` member 0, evolutions ``a/0/3/4`` p, battle/summary pictures
  ``a/0/0/4`` 6p..6p+5 and party icon ``a/0/2/0`` 7+p.
* Followers: ``a/0/2/8`` member 10 (u16 base overworld per species) and member 9
  (u8 overworld forms per species) into ``a/1/4/1``.

A species/form reference is ``{'species': base, 'form': f}``; the packed native
word is ``base | f << 11`` (trainer parties, wild slots, evolution targets).
Battle-only (reversion) forms, limbo/egg slots 494..543, non-regional forms and
rows with any missing record are refused with their reason.
"""
import struct

from .formats import require, member_count, member_span, file_span

MAX_MON_NUM = 1075                  # SPECIES_PECHARUNT; include/constants/species.h
LIMBO = range(494, 544)             # SPECIES_EGG 494 .. SPECIES_VICTINI 544 (exclusive)
FIRST_EXPANDED = 544
FORM_MEMBER, FORM_SPECIES_MEMBER, OW_COUNT_MEMBER, OW_BASE_MEMBER = 11, 12, 9, 10
ADDONS, PERSONAL, PICTURES, ICONS, OVERWORLD = 'a/0/2/8', 'a/0/0/2', 'a/0/0/4', 'a/0/2/0', 'a/1/4/1'
LEVELUP, EVOLUTION = 'a/0/3/3', 'a/0/3/4'
NEEDS_REVERSION = 0x8000
# Persistent regional variants (species.h *_REGIONAL_START ranges, checked against the
# ROM form table by tools/assets_gameplay_qualification.py).
# The Paldean block (1354..) mixes regional variants with other forms: only Wooper
# (1363) and the three Tauros breeds (1364..1366) are regional.
REGIONS = (('Alolan', 1126, 1156), ('Galarian', 1156, 1175), ('Hisuian', 1326, 1347),
           ('Paldean', 1363, 1367))
LABELS = {1364: 'Paldean Combat', 1365: 'Paldean Blaze', 1366: 'Paldean Aqua'}
# Galarian Darmanitan Zen (1174) changes form in battle; it is reached only by its ability.
BATTLE_FORMS = {1174}
SPECIES_BANK = 237


def _archive(project, path):
    cache = project.__dict__.setdefault('_species_form_archives', {})
    if path not in cache:
        cache[path] = file_span(project.blob, path)[1]
    return cache[path]


def _member(project, path, index):
    raw = _archive(project, path)
    if index >= struct.unpack_from('<H', raw, 24)[0]:
        return None
    return member_span(raw, index)[1]


def form_table(project):
    raw = _member(project, ADDONS, FORM_MEMBER)
    require(raw is not None and len(raw) == MAX_MON_NUM * 64, 'Unexpected form table size', 'UNSUPPORTED_RUNTIME')
    return raw


def form_entry(project, species, form):
    """Raw form-table word (0 = no such form)."""
    if not 1 <= form <= 31 or not 0 < species < MAX_MON_NUM:   # 1075 rows (0..1074)
        return 0
    return struct.unpack_from('<H', form_table(project), 2 * (32 * species + form - 1))[0]


def personal_index(project, species, form=0):
    """Mirror of PokeOtherFormMonsNoGet for regional/persistent forms (default branch)."""
    if form == 0:
        return species
    entry = form_entry(project, species, form) & ~NEEDS_REVERSION
    return entry or species


def region(index):
    return next((name for name, lo, hi in REGIONS if lo <= index < hi), None)


def _data_checks(project, species, index, form):
    from . import species as sp
    reasons = []
    personal = _member(project, PERSONAL, index)
    if personal is None or len(personal) != sp.PERSONAL_SIZE or not any(personal[0:6]):
        reasons.append('no personal record')
    elif not all(t in sp.TYPES for t in personal[6:8]):
        reasons.append('type outside the qualified type catalog')
    table = _member(project, LEVELUP, 0)
    try:
        if not sp.decode_learnset(table[index * sp.ROW_BYTES:(index + 1) * sp.ROW_BYTES]):
            reasons.append('empty learnset row')
    except Exception:
        reasons.append('learnset row outside the qualified layout')
    evolution = _member(project, EVOLUTION, index)
    if evolution is None or len(evolution) not in (sp.EVOLUTION_SIZE, 62):
        reasons.append('no evolution record')
    pics = [_member(project, PICTURES, index * 6 + k) for k in range(6)]
    if any(p is None or len(p) == 0 for p in pics):
        reasons.append('battle/summary pictures missing')
    icon = _member(project, ICONS, 7 + index)
    if icon is None or not icon:
        reasons.append('party icon missing')
    base_ow = struct.unpack_from('<H', _member(project, ADDONS, OW_BASE_MEMBER), 2 * species)[0]
    ow_forms = _member(project, ADDONS, OW_COUNT_MEMBER)[species]
    follower = 'form' if form and form < max(ow_forms, 1) and ow_forms > 1 else 'base'
    ow = _member(project, OVERWORLD, base_ow + (form if follower == 'form' else 0))
    if ow is None or not ow:
        reasons.append('follower sprite missing')
    return reasons, {'personal_index': index, 'pictures': [index * 6, index * 6 + 5], 'icon': 7 + index,
                     'follower_sprite': base_ow + (form if follower == 'form' else 0),
                     'follower': 'own form sprite' if follower == 'form' or not form else 'base-species sprite'}


def roster(project):
    """Every expanded base species and regional form with its qualification result."""
    cached = getattr(project, '_species_form_roster', None)
    if cached is not None:
        return cached
    from .gameplay import name
    rows = []
    for species in range(FIRST_EXPANDED, MAX_MON_NUM + 1):
        label = name(project, SPECIES_BANK, species)
        reasons, info = _data_checks(project, species, species, 0)
        if not label:
            reasons.append('no species name')
        rows.append({'key': f'{species}', 'species': species, 'form': 0, 'name': label or f'Species {species}',
                     'kind': 'expanded', 'supported': not reasons, 'reasons': reasons, **info})
    for species in range(1, MAX_MON_NUM + 1):
        if species in LIMBO:
            continue
        for form in range(1, 32):
            entry = form_entry(project, species, form)
            if not entry:
                break
            index = entry & ~NEEDS_REVERSION
            where = region(index)
            if where is None:
                continue
            reasons, info = _data_checks(project, species, index, form)
            if entry & NEEDS_REVERSION or index in BATTLE_FORMS:
                reasons.insert(0, 'battle-only form (reverts after battle)')
            base = name(project, SPECIES_BANK, species) or f'Species {species}'
            rows.append({'key': f'{species}:{form}', 'species': species, 'form': form,
                         'name': f'{base} ({LABELS.get(index, where)})', 'kind': 'regional form', 'region': where,
                         'supported': not reasons, 'reasons': reasons, **info})
    project._species_form_roster = rows
    return rows


def custom(project, species, form):
    """A bound custom Pokémon identity (POKE-02) at (species, form), from the state being composed
    during replay or the Project's composition otherwise."""
    if not form:
        return None
    from . import pokemon_packages as pp
    if getattr(project, '_composing', False):
        state = project.__dict__.get('_replaying') or {}
    else:
        state = project.composed()
    row = pp.lookup(state, species, form)
    if row is None:
        return None
    display = pp.packages(state).get(row['package'], {}).get('display', row['key'])
    return {'key': f'{species}:{form}', 'species': species, 'form': form, 'name': display, 'kind': 'custom form',
            'supported': True, 'reasons': [], 'personal_index': row['personal'], 'follower': 'own form sprite',
            'inherits_from': species, 'identity': row['key']}


def lookup(project, species, form=0):
    """The qualified row of a base species (1..493 stays the base catalog) or form."""
    require(type(species) is int and type(form) is int and not isinstance(species, bool),
            'Species and form must be integers', 'INVALID_INPUT')
    row = custom(project, species, form)
    if row is not None:
        return row
    if form == 0 and 0 < species <= 493:
        from .gameplay import entry
        e = entry(project, 'species', species)
        return {'key': str(species), 'species': species, 'form': 0, 'name': e['name'], 'kind': 'base',
                'supported': e['supported'], 'reasons': [e['reason']] if e['reason'] else [], 'personal_index': species}
    row = next((r for r in roster(project) if r['species'] == species and r['form'] == form), None)
    if row is None:
        reason = ('limbo/egg slot' if species in LIMBO else
                  'form is not a qualified regional variant' if form else 'outside the species catalog')
        return {'key': f'{species}:{form}' if form else str(species), 'species': species, 'form': form,
                'name': f'Species {species}' + (f' form {form}' if form else ''), 'supported': False,
                'reasons': [reason]}
    return row


def require_supported(project, species, form=0, what='Species'):
    row = lookup(project, species, form)
    require(row['supported'], f"{what} {row['key']} is not qualified: {'; '.join(row['reasons'])}", 'UNSUPPORTED_ID')
    return row


def pack(species, form):
    require(0 < species < 2048 and 0 <= form < 32, 'Species/form outside the packed native word', 'INVALID_INPUT')
    return species | form << 11


def unpack(word):
    return word & 0x7ff, word >> 11


def display(project, species, form=0):
    return lookup(project, species, form)['name']


def catalog(project, search='', offset=0, limit=40, include_unsupported=False):
    from . import pokemon_packages as pp
    rows = [r for r in roster(project) if include_unsupported or r['supported']]
    rows += [custom(project, i['species'], i['form']) for i in pp.identities(project.composed()).values()]
    if search:
        term = search.lower()
        rows = [r for r in rows if term in r['name'].lower() or term == r['key']]
    return {'kind': 'species_forms', 'total': len(rows), 'offset': offset,
            'scope': 'expanded base species 544..1075, persistent regional forms qualified from this ROM and '
                     'custom Pokémon identities; base species 1..493 remain in the species catalog',
            'entries': [{k: r[k] for k in ('key', 'species', 'form', 'name', 'kind', 'supported', 'reasons',
                                          'personal_index', 'follower')} for r in rows[offset:offset + limit]]}


def summary(project):
    rows = roster(project)
    return {'expanded_supported': sum(r['supported'] for r in rows if r['form'] == 0),
            'expanded_total': sum(r['form'] == 0 for r in rows),
            'forms_supported': sum(r['supported'] for r in rows if r['form']),
            'forms_total': sum(bool(r['form']) for r in rows),
            'follower_base_sprite_forms': sum(1 for r in rows if r['form'] and r['supported']
                                              and r['follower'] == 'base-species sprite')}
