"""Bounded native contracts, with explicit entry conditions; never write grants.

See evidence/m5-runtime-contracts/native-notes.md for instruction derivations.
These pure models do not run an emulator, inspect a save, or infer an event's
entry state. A conditional transfer is deliberately not a dynamic layout bound.
"""
import struct

from .formats import require


ACTIVE = 1
NORMAL_BUSY = 2
HELD = 0x10
FINISHED = 0x20
PAUSED = 0x40


def held_complete(flags):
    """02062198: no held action OR held completion bit. Ignores NORMAL_BUSY."""
    return not flags & HELD or bool(flags & FINISHED)


def stream_ready(flags):
    """02062108: active, normal movement idle, and no unfinished held action."""
    return bool(flags & ACTIVE) and not flags & NORMAL_BUSY and held_complete(flags)


def actor_update_lane(flags, manager_disabled=False, normal_gate=True):
    """Dispatch decision at 0205fd30, after the pre-update hooks.

    normal_gate represents 0205fd98 and 02063a1c; it cannot be assumed for
    arbitrary actors. The held lane precedes (and bypasses) normal pause flags.
    """
    if manager_disabled:
        return 'disabled'
    if flags & HELD:
        return 'held'
    if flags & (PAUSED | (1 << 30)) or not normal_gate:
        return 'paused'
    return 'normal'


def remap_player_action(action):
    """02065db4: remap 58..5b to 10..13; other MOVEMENT values pass through."""
    require(type(action) is int and 0 <= action <= 255, 'Invalid player action')
    return action - 0x48 if 0x58 <= action <= 0x5b else action


def enqueue_follower_action(phase, action, player_previous):
    """Overlay 1 02205990: one overwriting mailbox, not a movement FIFO."""
    require(type(phase) is int and 0 <= phase <= 3, 'Unknown follower queue phase')
    remap_player_action(action)  # validate, but the producer stores the original action
    target = tuple(player_previous)
    require(len(target) == 2 and all(type(v) is int for v in target), 'Invalid queue target')
    return {'phase':{0:1,1:1,2:2,3:2}[phase], 'action':action,'target':list(target)}


def follower_step(movement_type, follower, player_previous, player_action):
    """One *eligible* type-37/38 normal step, not a catch-up loop.

    Eligibility requires cached player coordinates to differ, state zero,
    normal lane admission, and visibility hooks to return. 02065d58 consumes
    the new player coordinate before the action starts. No eligibility is
    inferred here. Type 30 additionally depends on its queue/terrain state.
    """
    require(movement_type in (0x37, 0x38), 'Type 30 needs queue and terrain state',
            'FOLLOWER_QUEUE_STATE_REQUIRED')
    action = remap_player_action(player_action)
    require(12 <= action <= 19, 'Unbounded follower action family',
            'FOLLOWER_ACTION_UNRESOLVED')
    x, z = follower
    px, pz = player_previous
    if movement_type == 0x37:
        if (x, z) == (px, pz):
            return (x, z)
        # 02061200 prefers X, then Z. 0206234c preserves the speed family.
        direction = 2 if x > px else 3 if x < px else 0 if z > pz else 1
    else:
        # 02065fbc intentionally discards the coordinate getter results.
        direction = action % 4
    dx, dz = ((0, -1), (0, 1), (-1, 0), (1, 0))[direction]
    return x + dx, z + dz


def conditional_follower_sweep(stream, player_start, follower_starts, movement_type=0x37,
                               allow_missed_updates=True):
    """Finite transfer over a caller-supplied entry envelope.

    At most one normal step per changed player tile; optional missed updates
    overapproximate pause/delay/cache consumption. In that mode both the old
    and completed player tile are possible previous-coordinate samples, because
    0206254a sets previous=current at completion. Starting interpolation/held
    state and all other effects must be established upstream.
    """
    require(follower_starts is not None, 'Follower entry envelope is unproved',
            'FOLLOWER_ENTRY_STATE_REQUIRED')
    states = {tuple(c) for c in follower_starts}
    require(0 < len(states) <= 1024 and all(len(c) == 2 and all(type(v) is int for v in c)
                                         for c in states), 'Invalid follower envelope')
    require(movement_type in (0x37, 0x38), 'Type 30 needs queue and terrain state',
            'FOLLOWER_QUEUE_STATE_REQUIRED')
    starts = sorted(states)
    px, pz = player_start
    swept, steps = set(states), []
    for rec in stream['records']:
        op = rec['opcode']
        if rec['kind'] != 'step':
            require(rec['kind'] in ('face', 'delay', 'facing_lock'),
                    'Follower projection crosses an effect', 'FOLLOWER_EFFECT_UNRESOLVED')
            continue
        require(12 <= op <= 19 and 1 <= rec['repeats'] <= 256,
                'Unsupported player step', 'FOLLOWER_ACTION_UNRESOLVED')
        expected = ((0,-1),(0,1),(-1,0),(1,0))[op % 4]
        require(tuple(rec['tile_delta']) == expected, 'Player direction differs')
        for _ in range(rec['repeats']):
            samples = {(px,pz)}
            if allow_missed_updates:
                samples.add((px+expected[0],pz+expected[1]))
            advanced = {follower_step(movement_type,c,p,op) for c in states for p in samples}
            states = states | advanced if allow_missed_updates else advanced
            swept.update(states)
            px += expected[0]; pz += expected[1]
            steps.append({'player':[px,pz], 'follower_tiles':[list(c) for c in sorted(states)]})
            require(len(states) <= 4096 and len(steps) <= 4096, 'Follower transfer exceeds bound')
    return {'dynamic_qualified':False, 'movement_type':movement_type,
            'follower_starts':[list(c) for c in starts],
            'player_end':[px,pz], 'end_tiles':[list(c) for c in sorted(states)],
            'swept_tiles':[list(c) for c in sorted(swept)], 'steps':steps,
            'allow_missed_updates':allow_missed_updates,
            'conditions':['supplied entry envelope covers every possible follower coordinate',
                          'normal state zero and no pending interpolation/held action at entry',
                          'each normal launch maps to one observed player step/action; no pending action at entry',
                          'no other coordinate writer or lifecycle change; visibility hooks return'],
            'scope':'Conditional finite swept-tile transfer, not proof that its entry conditions hold.'}


def child_serialization(audit):
    """Prove the rooted child SCRIPT graphs have an atomic release/End tail.

    Native 0203ff44 visits slots 0..2 in order. 02040bfc toggles bit(index-1)
    and returns zero, so 0203fd6c executes the immediately following End in the
    same invocation. The parent cannot resume within that tail. We reject nested
    dispatch, malformed stacks, missing/double release and any other tail. Cycles
    before release remain a liveness limitation, not permission to skip the child.
    """
    commands = {(r['member'],r['offset']):r for r in audit['decoded']}
    roots = sorted({(r['dispatched_entry']['member'],r['dispatched_entry']['offset'])
                    for r in audit['decoded'] if r.get('kind') == 'dispatch'})
    results = []
    for member, start in roots:
        pending = [(start, ())]; visited = set(); tails = set(); failures = set(); early_ends = set()
        while pending:
            offset, stack = pending.pop()
            if (offset,stack) in visited:
                continue
            visited.add((offset,stack))
            if len(visited) > 4096 or len(stack) > 19:
                failures.add('CHILD_GRAPH_BOUND'); break
            rec = commands.get((member,offset))
            if rec is None or 'kind' not in rec:
                failures.add('CHILD_COMMAND_UNRESOLVED'); continue
            raw = bytes.fromhex(rec['before']); end = offset + len(raw); kind = rec['kind']
            if kind == 'child_release':
                tail = commands.get((member,end))
                if stack or tail is None or tail.get('before') != '0200' or tail.get('kind') != 'end':
                    failures.add('CHILD_RELEASE_TAIL_NOT_ATOMIC')
                else:
                    tails.add((offset,end))
                continue
            if kind == 'end':
                early_ends.add((offset,stack))
                failures.add('CHILD_END_WITHOUT_RELEASE'); continue
            if kind == 'dispatch':
                failures.add('NESTED_CHILD_DISPATCH'); continue
            if kind == 'return':
                if not stack: failures.add('CHILD_RETURN_WITHOUT_CALL')
                else: pending.append((stack[-1],stack[:-1]))
                continue
            if kind in ('branch','conditional_call'):
                pending.append((end + struct.unpack_from('<i',raw,3)[0],
                                stack+(end,) if kind == 'conditional_call' else stack))
            if kind in ('jump','call'):
                pending.append((end + struct.unpack_from('<i',raw,2)[0],
                                stack+(end,) if kind == 'call' else stack))
                continue
            pending.append((end,stack))
        if not tails: failures.add('CHILD_RELEASE_NOT_REACHED')
        results.append({'member':member,'offset':start,'serializable':not failures,
                        'release_tails_atomic':bool(tails) and not (failures-{'CHILD_END_WITHOUT_RELEASE'}),
                        'states':len(visited), 'release_end_pairs':[list(v) for v in sorted(tails)],
                        'early_end_sites':[{'offset':o,'return_stack':list(stk)} for o,stk in sorted(early_ends)],
                        'refusals':sorted(failures)})
    return {'roots':results,'complete':all(r['serializable'] for r in results),
            'context_slots':3, 'visit_order':[0,1,2], 'release_operation':'xor 1 << (child_index - 1)',
            'scope':'SCRIPT release ordering only; external tasks can run while either context waits. '
                    'No liveness, actor-state or transitive-effect guarantee.'}


# Coordinate-local contracts. Unproved asynchronous preconditions stay explicit
# in projection output rather than being silently reclassified as harmless.
COMMAND_CONTRACTS = {
    0x62: ('pause_actor', 'set actor flag 0x40; missing follower 253 tolerated',
           'actor remains present; in-flight held movement is not paused'),
    0x63: ('resume_actor', 'clear actor flag 0x40; missing follower 253 tolerated',
           'normal actor updates after resumption still need a swept-tile bound'),
    0x25a: ('follower_pause', 'if follower gate true, nonzero sets 0x40, zero clears it',
            'does not create, reveal or position the follower; future normal updates need bounds'),
    0x25b: ('follower_held_wait', 'poll !held || finished, only when follower gate true',
            'normal movement can still be active when this wait succeeds'),
    0x25c: ('follower_type', 'replace normal callbacks and reinitialize scratch; retain task priority',
            'does not synchronize coordinates or drain an in-flight normal action'),
    0x261: ('follower_reset', 'reset at current tile, set previous=current, cancel held state',
            'does not move follower to player; current coordinate must already be bounded'),
    0x2d9: ('follower_gate', 'write boolean field[0xfa] && party gate, not visibility or proximity',
            'gate does not supply coordinates or prove completion'),
}


def contract_sites(audit):
    return [{'member':r['member'],'offset':r['offset'],'opcode':r['opcode'],
             'contract':COMMAND_CONTRACTS[r['opcode']][0],
             'effect':COMMAND_CONTRACTS[r['opcode']][1],
             'remaining_condition':COMMAND_CONTRACTS[r['opcode']][2],
             'before':r['before']}
            for r in audit['decoded'] if r['opcode'] in COMMAND_CONTRACTS]
