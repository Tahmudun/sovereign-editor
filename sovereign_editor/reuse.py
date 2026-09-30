"""Clean production projects: reuse explicitly selected content from another project (PROD-01).

A clean project starts from the immutable ROM (``create``); nothing is copied implicitly.
The author selects reusable definitions from any other project — characters, trainers, named
states, custom prop assets, ground materials, Pokémon packages and their identities — and the
selection is closed over its dependencies (a trainer brings its character, defeat state and any
custom Pokémon identity in its party; an identity brings its package; an environment brings its
ground materials and prop assets). Every item is then
re-created in the clean project by its ordinary operation in one area edit, so the clean project
allocates its own IDs (trainer IDs, state variables/flags, model IDs, personal indexes) and
nothing of the source's history (load rooms, disposable teams, scenes) comes along.

Packages (props, ground materials, Pokémon) are content-addressed: the source project's
verified package files are staged unchanged, so the clean project holds the same sources.
Retired definitions are refused. Read-only on the source; Project owns writes to the target.
"""
import copy

from .formats import EditorError, require

KINDS = ('state', 'character', 'trainer', 'prop', 'ground', 'pokemon', 'identity', 'environment')
CONTEXT = {'header': 67, 'cell': [17, 12]}
ALLOCATED = {'trainer': ('trainer_id',), 'state': ('variable', 'flag'), 'character': ()}


def _catalogs(state):
    from . import story_authoring as st, props, ground_materials as gm, pokemon_packages as pp, environments as env
    return {'state': st.catalog(state, 'state'), 'character': st.catalog(state, 'character'),
            'trainer': st.catalog(state, 'trainer'), 'prop': props.assets(state), 'ground': gm.materials(state),
            'pokemon': pp.packages(state), 'identity': pp.identities(state), 'environment': env.catalog(state)}


def parse(items):
    out = []
    for item in items:
        require(isinstance(item, str) and ':' in item, 'Select items as kind:key', 'INVALID_INPUT')
        kind, key = item.split(':', 1)
        require(kind in KINDS, f"Reusable kinds: {', '.join(KINDS)}", 'INVALID_INPUT')
        out.append((kind, key))
    return out


def closure(source_state, selection):
    """[(kind, key, reason)] in dependency order: every selected item plus what it needs."""
    cats = _catalogs(source_state)
    chosen, order = {}, []

    def add(kind, key, reason):
        require(key in cats[kind], f'The source project has no {kind} {key}', 'NOT_FOUND')
        require(not cats[kind][key].get('retired'), f'{kind} {key} is retired in the source', 'RETIRED')
        if (kind, key) in chosen:
            return
        chosen[(kind, key)] = reason
        value = cats[kind][key]
        if kind == 'trainer':
            if value.get('character'):
                add('character', value['character'], f'character of trainer {key}')
            if value.get('defeat_state'):
                add('state', value['defeat_state'], f'defeat state of trainer {key}')
            for mon in value.get('party', []):
                for ident, spec in cats['identity'].items():
                    if (mon.get('species'), mon.get('form', 0)) == (spec['species'], spec['form']):
                        add('identity', ident, f'Pokémon in trainer {key}')
        elif kind == 'identity':
            add('pokemon', value['package'], f'package of identity {key}')
        elif kind == 'environment':
            for mid in value['ground']:
                add('ground', mid, f'ground material of environment {key}')
            for aid in value['props']:
                add('prop', aid, f'prop asset of environment {key}')
        order.append((kind, key))
    for kind, key in selection:
        add(kind, key, 'selected')
    rank = {k: i for i, k in enumerate(KINDS)}
    order.sort(key=lambda kk: rank[kk[0]])
    return [(k, key, chosen[(k, key)]) for k, key in order]


def _stage(target, module, key, sha, folder):
    """Stage the source project's verified package files unchanged for the clean project."""
    import json
    files = {path.relative_to(folder).as_posix(): path.read_bytes() for path in sorted(folder.rglob('*'))
             if path.is_file()}
    module.pending(target)[(key, sha)] = {'files': files, 'body': json.loads(files['manifest.json'])}


def plan(target, source, items):
    """Dry run: the dependency closure, what is skipped (already present) and the operations."""
    from . import props, ground_materials as gm, pokemon_packages as pp
    source_state, target_state = source.composed(), target.composed()
    cats, have = _catalogs(source_state), _catalogs(target_state)
    rows, operations = [], []
    for kind, key, reason in closure(source_state, parse(items)):
        value = cats[kind][key]
        if key in have[kind]:
            rows.append({'kind': kind, 'key': key, 'reason': reason, 'result': 'already in the clean project'})
            continue
        if kind in ('state', 'character', 'trainer'):
            request = {k: copy.deepcopy(v) for k, v in value.items() if k not in ALLOCATED[kind] + ('retired',)}
            if kind == 'state':
                request = {k: v for k, v in request.items() if k in ('name', 'switch')}
            operations.append({'kind': 'story', 'context': CONTEXT,
                               'request': {'kind': kind, 'key': key, 'value': request}})
        elif kind in ('prop', 'ground', 'pokemon'):
            module = {'prop': props, 'ground': gm, 'pokemon': pp}[kind]
            sha = value['package']
            module.verify_package(source, key, sha)
            _stage(target, module, key, sha, module.package_dir(source, key, sha))
            op = {'prop': {'action': 'register', 'asset': key, 'package': sha},
                  'ground': {'action': 'register', 'material': key, 'package': sha},
                  'pokemon': {'action': 'import', 'key': key, 'package': sha}}[kind]
            operations.append({'kind': {'prop': 'prop', 'ground': 'ground', 'pokemon': 'pokemon'}[kind],
                               'context': CONTEXT, 'request': op})
        elif kind == 'environment':
            # Presets are stored resolved (items, not the source groups/decals), so they travel as data.
            fields = {k: copy.deepcopy(value[k]) for k in ('display', 'kind', 'ground', 'props', 'presets', 'donors', 'notes')}
            operations.append({'kind': 'environment', 'context': CONTEXT,
                               'request': {'action': 'define', 'key': key, **fields}})
        else:
            operations.append({'kind': 'pokemon', 'context': CONTEXT,
                               'request': {'action': 'bind', 'key': key, 'package': value['package'],
                                           'evolution': value['evolution']}})
        rows.append({'kind': kind, 'key': key, 'reason': reason, 'result': 'created by its own operation'})
    return {'items': rows, 'operations': operations,
            'not_reused': 'history, created areas, load rooms, scenes and events stay in the source project'}


def apply(target, source, items, expected_revision, label='Reuse content'):
    result = plan(target, source, items)
    require(result['operations'], 'Everything selected is already in this project', 'NO_CHANGE')
    applied = target.apply_area_edit(expected_revision, operations=result['operations'], label=label)
    return {**result, 'revision': applied['revision']}
