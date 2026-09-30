"""Qualified persistent storage for named states and scene-actor visibility (PROD-CAP-001).

Everything lives in the stock SaveVarsFlags save block, so saves keep their format:
vars[0x170] (IDs 0x4000..0x416F) followed by flags[2912]. Nothing extends past
either array. Evidence for the exact pinned build is produced by
tools/storage_qualification.py (evidence/production-authoring-v2/storage-qualification.json):
every script (field and Battle Frontier) at any byte offset, event records,
resolved code call sites in ARM9 and all overlays, reviewed computed ranges,
19 played saves and the exact map-temp/daily clears.

* Number states hold 0..65535 in a variable: the historical 0x4160..0x416F slots
  first (their mapping never changes), then 44 variables with no consumer at all.
* Switch states hold on/off in a persistent flag of the event-flag region
  0x872..0x95F: above the trainer flags of every stock and authored trainer
  (0x550 + ID <= 0x871) and below system (0x960..) and daily (0xAA0..0xB5F) flags.
* Scene visibility keeps its 14 historical flags and adds 18 strict flags.

Pure data and guards; Project owns writes.
"""
import struct

from .formats import baseline_digest, file_span, require

LEGACY_STATE_VARS = tuple(range(0x4160, 0x4170))
# No script/trigger/code consumer, outside every computed range, zero in every played save.
EXTRA_STATE_VARS = (0x4031, 0x4034, 0x4059, 0x405a, 0x405b, 0x405c, 0x405d, 0x405e, 0x405f, 0x4061,
                    0x4062, 0x4063, 0x4064, 0x4065, 0x4066, 0x4067, 0x4069, 0x406a, 0x406b, 0x406c,
                    0x406d, 0x406e, 0x406f, 0x4093, 0x40c6, 0x40e4, 0x40f2, 0x4128, 0x413d, 0x413e,
                    0x413f, 0x4141, 0x4152, 0x4153, 0x4155, 0x4156, 0x4157, 0x4158, 0x4159, 0x415a,
                    0x415b, 0x415d, 0x415e, 0x415f)
STATE_VARS = LEGACY_STATE_VARS + EXTRA_STATE_VARS
# Strict: no script byte, event record, code call-site constant, computed range,
# played-save use or even an unrelated 32-bit code word.
STRICT_FLAGS = (0x873, 0x883, 0x885, 0x891, 0x894, 0x895, 0x8a0, 0x8af, 0x8c0, 0x8d0, 0x8d9, 0x8db,
                0x8e0, 0x8e1, 0x8ef, 0x8f3, 0x8f9, 0x8fc, 0x910, 0x928, 0x93b, 0x94b, 0x94d, 0x950,
                0x951, 0x959, 0x95b, 0x95c, 0x95d, 0x95f)
# Reviewed: as strict, except a matching 32-bit word that is table data or is
# loaded only by functions that never reach a var/flag primitive or wrapper.
REVIEWED_FLAGS = (0x874, 0x879, 0x88b, 0x88d, 0x88f, 0x893, 0x897, 0x898, 0x899, 0x89d, 0x89f, 0x8a3,
                  0x8a5, 0x8a8, 0x8a9, 0x8ab, 0x8ac, 0x8ad, 0x8b1, 0x8b3, 0x8b7, 0x8b8, 0x8b9, 0x8bb,
                  0x8bc, 0x8bd, 0x8bf, 0x8c1, 0x8c5, 0x8c7, 0x8c8, 0x8c9, 0x8cb, 0x8cd, 0x8cf, 0x8d1,
                  0x8d3, 0x8d7, 0x8d8, 0x8dc, 0x8e3, 0x8e4, 0x8e8, 0x8eb, 0x8ec, 0x8ed, 0x8f4, 0x8f5,
                  0x8f7, 0x8fb, 0x8ff, 0x907, 0x909, 0x90d, 0x90f, 0x915, 0x918, 0x919, 0x91b, 0x91c,
                  0x91d, 0x921, 0x923, 0x925, 0x927, 0x929, 0x92b, 0x92d, 0x934, 0x935, 0x937, 0x938,
                  0x939, 0x93c, 0x93d, 0x93f, 0x943, 0x945, 0x947, 0x949, 0x94c, 0x94f, 0x953, 0x955,
                  0x957, 0x958)
HIDE_FLAGS_V1 = (0x521, 0x525, 0x527, 0x529, 0x531, 0x533, 0x534, 0x539, 0x53b, 0x53d, 0x543, 0x545, 0x54c, 0x54d)
HIDE_FLAGS = HIDE_FLAGS_V1 + STRICT_FLAGS[:18]
STATE_FLAGS = STRICT_FLAGS[18:] + REVIEWED_FLAGS
EVENT_REGION = (0x872, 0x95F)
SCRIPT_ARCHIVES = ('a/0/1/2', 'a/1/8/2')   # field scripts, Battle Frontier scripts (overlay 80)
EVIDENCE = 'evidence/production-authoring-v2/storage-qualification.json'


def is_switch(definition):
    return bool(definition.get('switch'))


def qualify(project):
    """Exact-baseline guard for every non-legacy allocation (evidence in EVIDENCE)."""
    if getattr(project, '_storage_qualified', False):
        return
    from .character_runtime import BASELINE
    import ndspy.narc
    from . import world
    require(baseline_digest(project) == BASELINE, 'Extended states require the pinned baseline', 'UNSUPPORTED_RUNTIME')
    values = set(EXTRA_STATE_VARS) | set(HIDE_FLAGS[14:]) | set(STATE_FLAGS)
    for archive in SCRIPT_ARCHIVES:
        # A zero separator cannot fabricate a pool value across members: none ends in 0x00.
        joined = b'\0\0'.join(ndspy.narc.NARC(bytes(file_span(project.blob, archive)[1])).files)
        hits = [v for v in values if struct.pack('<H', v) in joined]
        require(not hits, f'Qualified storage occurs in {archive} scripts', 'STATE_CONFLICT')
    for raw in ndspy.narc.NARC(bytes(file_span(project.blob, world.EVENT_ARCHIVE)[1])).files:
        cursor = 0
        for size in (20, 32, 12, 16):
            n = struct.unpack_from('<I', raw, cursor)[0]
            cursor += 4
            for k in range(n):
                at = cursor + k * size
                if size == 32:
                    require(struct.unpack_from('<H', raw, at + 8)[0] not in values, 'Qualified flag belongs to a stock actor', 'STATE_CONFLICT')
                if size == 16:
                    require(struct.unpack_from('<H', raw, at + 14)[0] not in values, 'Qualified variable is a trigger condition', 'STATE_CONFLICT')
            cursor += n * size
    project._storage_qualified = True


def allocate_state(project, states, key, value, before):
    """``after`` for a v3 state definition: a stable variable or flag.

    An existing state keeps its storage; kinds never change. New number states
    take the next variable in STATE_VARS (legacy slots first), switches the next
    STATE_FLAGS entry. Library states cannot be deleted, so counts only grow.
    """
    require(isinstance(value, dict) and set(value) <= {'name', 'switch'} and 'name' in value
            and isinstance(value['name'], str) and 1 <= len(value['name']) <= 60,
            'Persistent state needs a display name', 'INVALID_INPUT')
    require(type(value.get('switch', False)) is bool, 'switch must be true or false', 'INVALID_INPUT')
    switch = value.get('switch', False)
    after = {'name': value['name']}
    if before is not None:
        require(is_switch(before) == switch, 'A state keeps its number/on-off kind once created', 'INVALID_INPUT')
        after.update({k: before[k] for k in ('switch', 'flag', 'variable') if k in before})
        return after
    used = [s for s in states.values() if is_switch(s) == switch]
    if switch:
        require(len(used) < len(STATE_FLAGS),
                f'All {len(STATE_FLAGS)} on/off state slots are used', 'RESOURCE_CAPACITY')
        after.update(switch=True, flag=STATE_FLAGS[len(used)])
    else:
        require(len(used) < len(STATE_VARS),
                f'All {len(STATE_VARS)} number state slots are used; use an on/off state', 'RESOURCE_CAPACITY')
        after['variable'] = STATE_VARS[len(used)]
    if (after.get('variable') or 0) not in LEGACY_STATE_VARS:
        qualify(project)
    return after


def capacity(states, sequences):
    numbers = sum(not is_switch(s) for s in states.values())
    switches = len(states) - numbers
    hidden = sum(bool(s.get('hide_flag')) for s in sequences.values())
    return {'number_states': {'used': numbers, 'limit': len(STATE_VARS)},
            'switch_states': {'used': switches, 'limit': len(STATE_FLAGS)},
            'named_states': {'used': len(states), 'limit': len(STATE_VARS) + len(STATE_FLAGS)},
            'visibility_actors': {'used': hidden, 'limit': len(HIDE_FLAGS)}}
