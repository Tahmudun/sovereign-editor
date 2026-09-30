"""Shared capacity and runtime-allocation reports (PROD-CAP-001), for UI and CLI.

Every limit here is enforced by the operation that would exceed it, before any
write; the report states the used/available counts and where that refusal comes
from. Read-only.
"""
import struct

from . import character_runtime as cr, story_authoring as story, storage, world, world_authoring, world_runtime, resident
from .formats import member_count


def _pool(used, limit, refusal):
    return {'used': used, 'limit': limit, 'available': max(0, limit - used), 'refusal': refusal}


def report(project):
    state = project.composed()
    states = story.catalog(state, 'state')
    sequences = story.catalog(state, 'sequence')
    trainers = story.catalog(state, 'trainer')
    held = storage.capacity(states, sequences)
    areas = world_authoring.areas(state)
    wild = member_count(project.blob, world_authoring.WILD) + sum(
        a == world_authoring.WILD for a, _ in project._world_members)
    ordinary = sum(bool(t.get('policy')) for t in trainers.values())
    limits = {
        'characters': _pool(len(story.catalog(state, 'character')), cr.MAX_CHARACTERS_V2,
                            'story character put: library slot (story v3)'),
        'trainers': {**_pool(len(trainers), cr.MAX_TRAINERS_V2, 'story trainer put: trainer IDs 738..801 (story v3)'),
                     'ordinary': ordinary, 'practice': len(trainers) - ordinary},
        'number_states': _pool(held['number_states']['used'], held['number_states']['limit'],
                               'state put: variables 0x4160..0x416F then 44 qualified'),
        'switch_states': _pool(held['switch_states']['used'], held['switch_states']['limit'],
                               'state put with switch: qualified event-region flags'),
        'named_states': _pool(len(states), held['named_states']['limit'], 'number + on/off pools'),
        'visibility_actors': _pool(held['visibility_actors']['used'], held['visibility_actors']['limit'],
                                   'presence scene put: hide flags (14 historical + 18 qualified)'),
        'created_headers': _pool(len(project._world_headers), world_runtime.MAX_HEADERS,
                                 'world create: header IDs 540..667 and resident overlay 129 space'),
        'area_cells': {**_pool(max((len(a['cells']) for a in areas.values()), default=0), world_authoring.MAX_CELLS,
                               f'world create: up to a {world_authoring.MAX_SIDE}x{world_authoring.MAX_SIDE} rectangle'),
                       'aggregate_created_cells': sum(len(a['cells']) for a in areas.values())},
        'private_encounter_tables': _pool(wild, world_authoring.NO_ENCOUNTERS,
                                          'world create with template encounters: u8 header field, 255 = none'),
    }
    from . import world_identity
    q = world_identity.qualified(project)
    limits.update({
        'location_names': _pool(len(world_identity.sections(state)), world_identity.SECTION_LIMIT - q['section_base'],
                                f'world identity name: map sections {q["section_base"]}..255 (8-bit header field, msg 279)'),
        'town_map_descriptions': _pool(len(world_identity.descriptions(state)),
                                       world_identity.DESCRIPTION_LIMIT - q['description_base'],
                                       f'world identity town_map.description: msg 273 IDs {q["description_base"]}..255'),
        'town_map_locations': _pool(len(world_identity.town_specs(project, state)), world_runtime.MAX_TOWN_SPECS,
                                    'world identity town_map: resident created Pokégear specs'),
        'static_members': _pool(len(world_identity.static_list(state)), world_runtime.MAX_STATIC,
                                'world animation / create static: resident animation-exclusion list'),
    })
    return {'revision': project.doc['revision'], 'limits': limits, 'resident': resident_summary(project, state),
            'batch_operations': 64,
            'notes': ['Library capacity is not per-map capacity: each map keeps its 0x800-byte event buffer, '
                      '255 local messages and 60 KiB map model limits.',
                      'Old projects keep resident layout v1 (8 characters, 32 trainers, 32 headers) byte for byte.']}


def resident_summary(project, state):
    plan = runtime(project, state)
    info = cr.overlay(project.blob, 129)
    data = plan['files'].get(info['file_id'], info['data']) if plan else info['data']
    return {'layout': 2 if plan and plan.get('resident') else 1, 'bytes': len(data), 'limit': resident.RESERVATION,
            'free': resident.RESERVATION - len(data) if not (plan and plan.get('resident')) else plan['resident']['free_bytes'],
            'stock_bytes': len(info['data'])}


def runtime(project, state):
    if not (project._world_headers or story.catalog(state, 'character') or story.catalog(state, 'trainer')):
        return None
    return world_authoring.runtime_plan(project, state)


def runtime_report(project):
    """Consolidated allocation after all character, scene, battle and world additions."""
    state = project.composed()
    plan = runtime(project, state)
    info = cr.overlay(project.blob, 129)
    field = cr.overlay(project.blob, 131)
    ext = plan['files'].get(info['file_id'], info['data']) if plan else info['data']
    ov131 = plan['files'].get(field['file_id'], field['data']) if plan else field['data']
    if plan and plan.get('resident'):
        resident_view = {**plan['resident'], 'layout': 2, 'limit': resident.RESERVATION}
    else:
        resident_view = {'layout': 1, 'overlay': 129, 'address': info['address'], 'bytes': len(ext),
                         'stock_bytes': len(info['data']), 'limit': resident.RESERVATION,
                         'free_bytes': resident.RESERVATION - len(ext), 'lifecycle': resident.LIFECYCLE,
                         'items': 'historical tail layout: additions appended in order after the stock image'}
    patches = [{'kind': p['kind'], 'rom_offset': p['rom_offset'], 'bytes': len(p['before']) // 2}
               for p in (plan['patches'] if plan else [])]
    return {'revision': project.doc['revision'], 'overlay_129': resident_view,
            'overlay_131': {'address': field['address'], 'bytes': len(ov131), 'stock_bytes': len(field['data']),
                            'limit': cr.FIELD_LIMIT, 'free_bytes': cr.FIELD_LIMIT - len(ov131),
                            'lifecycle': 'field only: overlay 130 (battle) replaces it; holds the overworld table '
                                         'and collection code, never resident lookups'},
            'patches': patches, 'created_headers': (plan or {}).get('world_headers'),
            'animation_exclusion': (plan or {}).get('world_static'), 'town_map': (plan or {}).get('world_town'),
            'appended_members': {k: len(v) for k, v in (plan['appends'] if plan else {}).items()}}
