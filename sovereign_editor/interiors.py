"""Independent stock room copies; append resources and rebind one existing header.

The pinned ROM's ARM9 is uncompressed. Only the two-byte matrix reference changes;
executable relocation/compression and new header allocation are deliberately absent.
Event/script/text resources must already be private to the selected house.
"""
import copy
import struct
from . import authoring, world, scenery, surface_authoring
from .formats import require, resource, map_sections, digest, EditorError

SCHEMA = 'sovereign-interior-copy-v1'


def reset(project):
    project.arm9 = project._base_arm9
    project._room_members = {}
    project._room_matrices = {}
    project._room_headers = {}
    project._context_cache = {}
    project._member_cache = {}
    for key in ('_area_resource_users', '_library_source_cache', '_event_users'):
        project.__dict__.pop(key, None)


def map_bytes(project, context, state):
    raw = project.member_raw(context['map_member'])
    sec = map_sections(raw)
    records = b''.join(o['raw'] for o in scenery.table_for(project, context, state).values())
    model = surface_authoring.model(project, context, state)
    result = bytearray(raw[:sec['buildings_offset']] + records + model + raw[sec['terrain_offset']:])
    struct.pack_into('<II', result, 4, len(records), len(model))
    for (member, offset), value in state['permissions'].items():
        if member == context['map_member']:
            result[offset:offset + 2] = value
    return bytes(result)


def plan(project, context, state, index, label=None):
    from .area_layout import resource_users
    h = context['header']; hid = h['id']
    require(hid not in project._room_headers, 'This interior already has an independent copy', 'ALREADY_INDEPENDENT')
    require(context['area_data']['area_type'] == 0 and context['matrix']['width'] == 1
            and context['matrix']['height'] == 1, 'Choose a single-cell stock interior', 'UNSUPPORTED_INTERIOR')
    users = resource_users(project, context)
    require(all(users[k] == [hid] for k in ('events', 'scripts', 'texts')),
            'This room shares events, scripts or text; independent logic allocation is not supported', 'SHARED_RESOURCE')
    require(not any(s['context']['header'] == hid for s in state.get('simple_interactions', {}).values()),
            'Make the room independent before adding authored interactions', 'BOUND_EVENT')
    require(not any(g['context']['header'] == hid for g in state.get('groups', {}).values()),
            'Make the room independent before saving linked groups', 'BOUND_OBJECT')
    start, _, _, size = struct.unpack_from('<4I', project.blob, 0x20)
    require(project.blob[start:start + size] == project._base_arm9,
            'Interior rebinding requires the qualified uncompressed ARM9', 'UNSUPPORTED_ROM')
    require(label is None or isinstance(label, str) and 0 < len(label) <= 160, 'Invalid room label')
    map_id = world.member_count(project.blob, world.MAP_ARCHIVE) + len(project._room_members)
    matrix_id = world.member_count(project.blob, world.MATRIX_ARCHIVE) + len(project._room_matrices)
    require(max(map_id, matrix_id) < 65535, 'Room resource capacity exceeded', 'RESOURCE_CAPACITY')
    source = map_bytes(project, context, state)
    matrix = resource(project.blob, world.MATRIX_ARCHIVE, h['matrix'])[1]
    deps = {'context': authoring.dependencies(context, index), 'source_sha256': digest(source),
            'users': users, 'matrix_sha256': digest(matrix)}
    return {'schema': SCHEMA, 'index': index, 'context': authoring.context_ref(context),
            'label': label or 'Make this interior independent', 'request': {'label': label},
            'map_member': map_id, 'matrix_member': matrix_id, 'dependencies': deps,
            'dependencies_sha256': authoring.canonical(deps)}


def replay(project, state, t, index):
    try:
        ctx = project.context(header=t['context']['header'], cell=t['context']['cell'])
        require(t == plan(project, ctx, state, index, **t['request']),
                'Interior copy dependencies changed', 'BEFORE_VALUE_MISMATCH')
        project._room_members[t['map_member']] = map_bytes(project, ctx, state)
        raw = bytearray(resource(project.blob, world.MATRIX_ARCHIVE, ctx['matrix']['id'])[1])
        # A single-cell matrix's map-member word is its final two bytes.
        struct.pack_into('<H', raw, len(raw) - 2, t['map_member'])
        project._room_matrices[t['matrix_member']] = bytes(raw)
        code = bytearray(project.arm9)
        struct.pack_into('<H', code, ctx['header']['arm9_offset'] + 4, t['matrix_member'])
        project.arm9 = bytes(code)
        project._room_headers[ctx['header']['id']] = copy.deepcopy(t)
        project._context_cache = {}
        for key in ('_area_resource_users', '_library_source_cache'):
            project.__dict__.pop(key, None)
        fresh = project.context(header=ctx['header']['id'], cell=t['context']['cell'])
        state['contexts'].append(fresh)
    except (KeyError, TypeError, ValueError, struct.error) as exc:
        raise EditorError('INVALID_INPUT', 'Malformed interior transaction') from exc


def summary(t):
    return {'operation': 'interior.copy', 'index': t['index'], 'label': t['label'],
            'context': t['context'], 'map_member': t['map_member'], 'matrix_member': t['matrix_member']}
