"""Read-only native route research for the pinned Cherrygrove baseline.

These definitions distinguish stream decoding, conditional coordinate projection,
and dynamic safety. None supplies a write capability. Native audit/provenance is
in evidence/m5-blocker-closure; no emulator execution is represented here.
"""
import struct

import ndspy.codeCompression

from .formats import digest, require, resource, span
from .runtime_contracts import COMMAND_CONTRACTS, child_serialization


def overlay_image(blob, index):
    """Read a ROM overlay by its allocation record, without assuming RAM residency."""
    offset, size = struct.unpack_from('<2I', blob, 0x50)
    rows = list(struct.iter_unpack('<8I', span(blob, offset, size)))
    matches = [(i, r) for i, r in enumerate(rows) if r[0] == index]
    require(len(matches) == 1, 'Overlay identity is ambiguous')
    i, row = matches[0]
    fat, fat_size = struct.unpack_from('<2I', blob, 0x48)
    lo, hi = struct.unpack_from('<2I', span(blob, fat, fat_size), row[6]*8)
    raw = span(blob, lo, hi-lo)
    data = ndspy.codeCompression.decompress(raw) if row[7] >> 24 & 1 else raw
    require(len(data) == row[2], 'Overlay decompressed size differs')
    return data, {'overlay':index, 'ram_address':row[1], 'rom_offset':lo,
                  'size':len(raw), 'sha256':digest(raw), 'decompressed_sha256':digest(data),
                  'table_offset':offset+i*32, 'table_before':span(blob,offset+i*32,32).hex(),
                  'residency':'Field overlay candidate; RAM address alone is not an identity proof.'}


def native_evidence(blob, arm9, shapes, opcodes):
    """Pin every consumed handler and the transitive slices actually researched.

    Recording a byte slice is provenance, not a semantic certification of all
    instructions in that slice. Overlay identity/residency remains explicit.
    """
    overlay, ref = overlay_image(blob,1)
    def read(offset,size):
        if offset < len(arm9):
            data = span(arm9,offset,size); image,local = 'arm9',offset
        else:
            local = offset-(ref['ram_address']-0x2000000)
            data = span(overlay,local,size); image = 'overlay:1'
        return {'image':image,'offset':local,'ram_address':0x2000000+offset,
                'size':size,'before':data.hex(),'sha256':digest(data)}
    pointers = sorted({(v[0]&~1)-0x2000000 for v in struct.iter_unpack('<I',span(arm9,0xfad00,853*4))})
    commands = []
    for op in sorted(opcodes):
        ptr = struct.unpack_from('<I',arm9,0xfad00+op*4)[0]
        start = (ptr&~1)-0x2000000
        end = next(p for p in pointers if p>start)
        commands.append({'opcode':op,'namespace':'script','operand_bytes':shapes[op][0],
                         'flow':shapes[op][1],'handler':ptr,'dispatch':read(0xfad00+op*4,4),
                         'handler_bytes':read(start,end-start),
                         'qualification':'cursor consumption/control shape; transitive effects separately unresolved'})
    ranges = [(0x4a90,0x4a9c),(0x5eb4,0x5f08),(0x6184,0x6190),
              (0x3fd18,0x3fe74),(0x3b8c4,0x3b91c),(0x40114,0x4018c),
              (0x40374,0x403d8),(0x40734,0x40890),(0x409d4,0x409e8),
              (0x40b68,0x40c44),(0x41b04,0x41b74),(0x41c70,0x41d40),
              (0x41f60,0x41fb8),(0x547d8,0x54824),(0x5489c,0x54940),
              (0x5c564,0x5c694),(0x5c6dc,0x5c6e4),(0x5ee60,0x5eef4),
              (0x5f20c,0x5f2d0),(0x5f370,0x5f450),(0x5f4cc,0x5f4fc),
              (0x5f6fc,0x5f714),(0x5f8e4,0x5f9a0),(0x5fb00,0x5fb38),
              (0x5fc2c,0x5fcb4),(0x5ece0,0x5ed18),(0x5fd20,0x5fd98),
              (0x60f0c,0x611f4),(0x613a0,0x616c0),(0x62108,0x6234c),
              (0x62400,0x626ac),(0x627b0,0x62918),(0x62d54,0x62de0),
              (0x62ef0,0x62fac),(0x658d4,0x65ffc),(0x69f88,0x69fb0),
              (0xfc604,0xfc61c),(0xfd1f4,0xfd2d8),(0xfd49c,0xfd4bc),
              (0xfe404,0xfe434),(0xfac9c,0xfacae),
              (0x1f654c,0x1f65f0),(0x2003f4,0x2006a8),(0x2092dc,0x2092f0)]
    return {'overlay':ref,'script_commands':commands,
            'researched_slices':[read(a,b-a) for a,b in ranges],
            'context_layout_correction':{'command_table':0x5c,'command_count':0x60,
                'message_data':0x78,'script_data':0x7c,
                'previous_notes':'Prior +0x74/+0x78 table/count labels were incorrect; native instructions use +0x5c/+0x60.'},
            'scope':'Exact native provenance. Full runtime effect closure and overlay residency are not certified.'}


def runtime_evidence(blob, arm9):
    """Additional explicitly selected images/slices, never RAM-address guessing.

    Overlay 27 selection is supported by overlay-1's explicit load of 27.
    Overlay 129 contains a patched follower sprite selector, whose transitive
    calls remain open. Full residency/lifetime is not implied by either fact.
    """
    images = {'arm9':(arm9,0x02000000)}; overlays = []
    for index in (1,27,129):
        data,ref = overlay_image(blob,index)
        images[f'overlay:{index}'] = (data,ref['ram_address']); overlays.append(ref)
    ranges = {
        'arm9':[(0x7524,0x7540),(0xe320,0xe33c),(0x1f880,0x1f990),(0x3df00,0x3df34),
                 (0x3fd18,0x3fdd8),(0x3ff44,0x4001c),(0x4005c,0x40114),
                 (0x401b4,0x402f0),(0x40374,0x403ac),(0x4055c,0x40614),
                 (0x403fc,0x40438),(0x40734,0x407ac),(0x40b68,0x40c2c),
                 (0x41d40,0x42034),(0x46cb4,0x46d5c),(0x46e5c,0x46e80),
                 (0x475c0,0x475f0),(0x476b4,0x477f8),(0x4ebb0,0x4ebe8),
                 (0x53038,0x5316c),(0x5316c,0x5320c),(0x5323c,0x53284),
                 (0x56358,0x5638e),(0x5d2d0,0x5d340),(0x5d5ce,0x5d610),
                 (0x5e1d0,0x5e294),(0x5e34c,0x5e38c),(0x5e3cc,0x5e420),
                 (0x5eaf0,0x5eb2c),(0x5ebfc,0x5ec90),(0x5ece0,0x5ed18),
                 (0x5f12c,0x5f148),(0x5f20c,0x5f228),(0x5f26c,0x5f280),
                 (0x5f370,0x5f398),(0x5f414,0x5f450),(0x5f574,0x5f5d4),
                 (0x5f630,0x5f73c),(0x5f8fc,0x5f944),(0x5fc2c,0x5fcb4),
                 (0x5fd20,0x5fe0c),(0x60f24,0x60fa8),(0x61070,0x61108),
                 (0x61200,0x6121c),(0x62108,0x621f0),(0x62214,0x6245c),
                 (0x624cc,0x62568),(0x63a14,0x63a94),(0x63afc,0x63b08),
                 (0x658d4,0x65ffc),(0x664d8,0x6659c),(0x699f8,0x69d70),
                 (0x69dc8,0x69fb0),(0x6a040,0x6a080),(0x6a128,0x6a1d4),
                 (0x77ce8,0x77cf0),(0x77d88,0x77db4),(0xfa560,0xfa570),
                 (0x10f168,0x10f170),(0x10f254,0x10f258),
                 (0xfcd74,0xfcd88),(0xfcdec,0xfce00),(0xfd0a8,0xfd0bc),
                 (0xfd198,0xfd1f4),(0xfe104,0xfe108),(0xfe134,0xfe138),
                 (0xfe164,0xfe168),(0xfe404,0xfe434)],
        'overlay:1':[(0x1f6894,0x1f6b20),(0x206c60,0x206ca0),
                     (0x20329c,0x20335c),(0x205990,0x205a2c),
                     (0x1e9270,0x1e92a8),(0x1e99f6,0x1e9a30)],
        'overlay:27':[(0x25c250,0x25c41c)],
        'overlay:129':[(0x3d9038,0x3d9068),(0x3d9234,0x3d92ca),
                       (0x3db894,0x3db928),(0x3de468,0x3de480),(0x3de7d8,0x3de7ea)],
    }
    slices = []
    for name,intervals in ranges.items():
        data,ram = images[name]
        for lo,hi in intervals:
            address = 0x02000000+lo; offset = address-ram
            raw = span(data,offset,hi-lo)
            slices.append({'image':name,'ram_address':address,'offset':offset,'size':hi-lo,
                           'before':raw.hex(),'sha256':digest(raw)})
    return {'overlays':overlays,'slices':slices,
            'scope':'Instruction provenance for the bounded contracts. Unexamined transitive effects '
                    'and whole-lifecycle overlay residency remain explicit obligations.'}


def item_runtime_inputs(blob, arm9):
    """Bounded hidden-item setup and SCRIPT 82 results for the rooted callers.

    0204005c -> 020405ac sets shared 8000..8002 from fa558. Patched
    02077ce8 -> overlay 129 023d9038 loads archive 17 by item ID directly;
    023d9234 attribute 5 returns (u16[8] >> 7) & 15. No vanilla item
    remapping or save inventory is assumed.
    """
    pointer = struct.unpack_from('<I',arm9,0x10f254)[0]-0x02000000
    require(span(arm9,pointer,8) == b'a/0/1/7\0', 'Native item archive differs')
    initial = {}; rows = []
    for sid in (8001,8225):
        matches = [(i,span(arm9,0xfa558+i*8,8)) for i in range(231)
                   if struct.unpack_from('<H',arm9,0xfa55e+i*8)[0] == sid-8000]
        require(len(matches)==1, 'Hidden item setup is ambiguous')
        index,raw = matches[0]
        initial[str(sid)] = {'32768':struct.unpack_from('<H',raw)[0],
                             '32769':raw[2], '32770':sid-7200}
        rows.append({'script_id':sid,'arm9_offset':0xfa558+index*8,'before':raw.hex()})
    items = {}
    for item in sorted({243} | {v['32768'] for v in initial.values()}):
        offset,raw = resource(blob,'a/0/1/7',item)
        items[str(item)] = {'archive':'a/0/1/7','member':item,'rom_offset':offset,
                            'before':raw.hex(),'sha256':digest(raw),
                            'pocket':struct.unpack_from('<H',raw,8)[0] >> 7 & 15}
    return {'event_variables':initial,'setup_rows':rows,'items':items,
            'scope':'Exact initial operands and item result under the pinned loader/attribute contract; '
                    'external task effects and overlay lifetime are not certified.'}


def initialization(raw):
    """Native 0407e4/04080c: five-byte headers, zero byte terminates.

    Type 1 has a relative conditional-table pointer. Other types carry a u16
    script ID, with two preserved bytes. This bounded member has only type 2.
    """
    cursor, records = 0, []
    while span(raw,cursor,1) != b'\0':
        item = span(raw,cursor,5)
        require(item[0] != 1, 'Conditional initializer requires its variable table', 'INIT_CONDITIONAL_UNRESOLVED')
        records.append({'offset':cursor, 'type':item[0], 'script_id':struct.unpack_from('<H',item,1)[0],
                        'before':item.hex(), 'reserved':item[3:].hex()})
        cursor += 5
    return {'member':623, 'records':records, 'terminator_offset':cursor,
            'trailing_before':raw[cursor:].hex(), 'dispatch_decoded':True,
            'native_lookup':0x020407e4, 'type_1_present':False}


def actor_reference(operand):
    if operand >= 0x4000:
        return {'kind':'variable', 'variable':operand, 'resolved':False}
    if operand == 0xf1:
        return {'kind':'context_object', 'context_slot':11, 'resolved':False}
    if operand == 0xf2:
        return {'kind':'movement_type_lookup', 'movement_type':48, 'resolved':False}
    return {'kind':'object_id', 'id':operand, 'role':{0:'guide',4:'rival',5:'resident',
             0xff:'player',0xfd:'follower'}.get(operand,'map actor'), 'resolved':True,
            'presence_required':True}


# Action namespace, not SCRIPT opcodes. All directions come from fd4ac/fd49c;
# start/update handlers increment tile coordinates then interpolate 65536 units.
ACTION_START = {0:0x6249c,1:0x624a8,12:0x62608,13:0x62620,14:0x62634,15:0x62648,
                16:0x6265c,17:0x62670,18:0x62684,19:0x62698,
                32:0x62898,33:0x628a8,34:0x628b8,35:0x628c8,
                36:0x628d8,37:0x628e8,38:0x628f8,39:0x62908,
                62:0x62da4,63:0x62db0,66:0x62dd4,71:0x62ef0,72:0x62f04,75:0x62f94}


def movement_definitions(arm9):
    dx = struct.unpack_from('<4i',arm9,0xfd4ac)
    dz = struct.unpack_from('<4i',arm9,0xfd49c)
    require(dx == (0,0,-1,1) and dz == (-1,1,0,0), 'Native direction table differs')
    result = {}
    for op, expected in ACTION_START.items():
        table = struct.unpack_from('<I',arm9,0xfd2d8+op*4)[0]-0x2000000
        moving = 12 <= op <= 19
        stages = 2 if op in (0,1,71,72) else 3
        handlers = list(struct.unpack_from('<'+'I'*stages,arm9,table))
        require(handlers[0] == 0x2000001+expected, 'Native movement dispatch differs')
        direction = op % 4 if moving or 32 <= op <= 39 or op in (0,1) else None
        kind = ('step' if moving else 'face' if direction is not None else
                'delay' if op in (62,63,66) else 'facing_lock' if op in (71,72) else 'effect')
        frames = (8 if op < 16 else 4) if moving else None
        result[op] = {'opcode':op, 'namespace':'movement', 'kind':kind,
                      'tile_delta':[dx[direction],dz[direction]] if moving else [0,0],
                      'direction':direction, 'interpolation_frames':frames,
                      'table_offset':table, 'table_before':span(arm9,table,stages*4).hex(),
                      'handlers':handlers,
                      'completion':'native action state reaches terminal bit; stream repeats only after completion',
                      'dynamic_qualified':False}
    return result


def movement_stream(raw, offset, definitions, limit=256):
    records, cursor, cells = [], offset, [(0,0)]
    for _ in range(limit):
        op, repeats = struct.unpack_from('<2H',span(raw,cursor,4))
        before = span(raw,cursor,4).hex()
        if op == 254:
            require(records and repeats == 0, 'Unsupported empty/malformed movement terminator', 'MOVEMENT_STREAM_INVALID')
            return {'offset':offset, 'end_offset':cursor+4, 'before':raw[offset:cursor+4].hex(),
                    'records':records, 'terminator':{'offset':cursor,'before':before},
                    'relative_swept_tiles':[list(c) for c in cells], 'tile_delta':list(cells[-1]),
                    'stream_decoded':True, 'dynamic_qualified':False}
        require(op in definitions, f'Unknown movement action {op:#x} at {cursor:#x}', 'UNKNOWN_MOVEMENT_ACTION')
        require(1 <= repeats <= 256, 'Unsupported movement repeat bound', 'MOVEMENT_STREAM_INVALID')
        action = definitions[op]
        records.append({'offset':cursor,'opcode':op,'repeats':repeats,'before':before,
                        'kind':action['kind'],'tile_delta':action['tile_delta']})
        dx,dz = action['tile_delta']
        for _ in range(repeats):
            if dx or dz:
                x,z = cells[-1]; cells.append((x+dx,z+dz))
        cursor += 4
    require(False, 'Movement stream exceeds bounded limit', 'MOVEMENT_STREAM_INVALID')


def project_event_routes(audit, streams, event_view, arm9):
    """Conditional X/Z projection of triggers and every NPC/background script.

    This intentionally does not certify general event/follower/battle execution.
    It honors native comparisons, assignments, explicit actor repositioning and
    asynchronous action groups, but projects through other effects under explicit
    assumptions. Such projections can reveal hazards, never establish safety.
    """
    commands = {(r['member'],r['offset']):r for r in audit['decoded']}
    child_contract = child_serialization(audit)
    serial_children = {(r['member'],r['offset']) for r in child_contract['roots'] if r['release_tails_atomic']}
    by_stream = {(s['member'],s['offset']):s for s in streams}
    condition_table = [list(arm9[i:i+3]) for i in range(0xfac9c,0xfacae,3)]
    launches, failures, assumptions, visited, endpoints = {}, [], set(), set(), []
    contracts_used = {}
    scenarios = []
    for root in audit['event_dispatch']:
        event = next(e for e in event_view[root['kind']] if e['id']==root['id'])
        if root['kind']=='triggers':
            for x in range(event['x'],event['x']+event['width']):
                for z in range(event['z'],event['z']+event['height']):
                    scenarios.append((f"trigger:{event['id']}@{x},{z}",root,(x,z),None,None))
        else:
            rx,rz = (event['range_x'],event['range_z']) if root['kind']=='npcs' else (0,0)
            for x in range(event['x']-rx,event['x']+rx+1):
                for z in range(event['z']-rz,event['z']+rz+1):
                    for dx,dz,facing in ((-1,0,3),(1,0,2),(0,-1,1),(0,1,0)):
                        name = f"{root['kind']}:{event['id']}@{x},{z}/player:{x+dx},{z+dz}"
                        scenarios.append((name,root,(x+dx,z+dz),facing,(x,z)))
    for name,root,player,initial_facing,interaction in scenarios:
        initial = {n['id']:(n['x'],n['z']) for n in event_view['npcs']}
        if root['kind']=='npcs': initial[root['id']] = interaction
        initial[255] = player
        # Serialize only when every rooted release/End tail is native-atomic.
        # Compare state is context-local; field/save variables are shared.
        variables = {int(k):v for k,v in audit.get('item_runtime',{}).get('event_variables',{}).get(
            str(root['script_id']),{}).items()}
        pending = [(root['member'],root['offset'],variables,initial,{},None,())]
        count = 0
        while pending:
            member,offset,variables,actors,moving,compare,stack = pending.pop()
            key = (name,member,offset,tuple(sorted(variables.items())),tuple(sorted(actors.items())),
                   tuple(sorted(moving.items())),compare,stack)
            if key in visited: continue
            visited.add(key); count += 1
            require(count <= 4096 and len(stack) <= 20, 'Route projection bound exceeded', 'ROUTE_PROJECTION_BOUND')
            rec = commands.get((member,offset))
            if rec is None or 'kind' not in rec:
                failures.append({'code':'PROJECTED_SCRIPT_UNRESOLVED','scenario':name,
                                 'member':member,'offset':offset})
                continue
            op = rec['opcode']; raw = bytes.fromhex(rec['before'])
            end = offset+len(raw); kind = rec['kind']
            vals = struct.unpack('<'+'H'*((len(raw)-2)//2),raw[2:]) if len(raw)%2 == 0 else ()
            def value(v): return v if v < 0x4000 else variables.get(v)
            def enqueue(m,at,callstack=stack):
                pending.append((m,at,dict(variables),dict(actors),dict(moving),compare,callstack))
            if kind == 'end':
                if moving:
                    failures.append({'code':'PROJECTED_END_WITH_MOVEMENT','scenario':name,'member':member,'offset':offset})
                if stack:
                    m,at,frame,saved_compare,released = stack[-1]
                    if any(f[2] == 'child' for f in stack) and (frame != 'child' or not released):
                        failures.append({'code':'PROJECTED_CHILD_END_WITHOUT_RELEASE','scenario':name,
                                         'member':member,'offset':offset})
                        continue
                    if frame == 'child':
                        compare = saved_compare
                        enqueue(m,at,stack[:-1])
                    else:
                        # Native End terminates the context, even inside a local call.
                        endpoints.append({'scenario':name,'actors':{str(a):list(c) for a,c in sorted(actors.items())}})
                else:
                    endpoints.append({'scenario':name,'actors':{str(a):list(c) for a,c in sorted(actors.items())}})
                continue
            if kind == 'return':
                if stack:
                    m,at,frame,_,_ = stack[-1]
                    if frame != 'local':
                        failures.append({'code':'PROJECTED_CHILD_RETURN_WITHOUT_CALL','scenario':name,
                                         'member':member,'offset':offset})
                        continue
                    enqueue(m,at,stack[:-1])
                else:
                    failures.append({'code':'PROJECTED_RETURN_WITHOUT_CALL','scenario':name,'member':member,'offset':offset})
                continue
            if kind in ('branch','conditional_call'):
                cond = raw[2]
                require(cond < 6, 'Invalid native comparison selector')
                take = bool(condition_table[cond][compare]) if compare is not None else None
                if take is not True: enqueue(member,end)
                if take is not False:
                    target = end+struct.unpack_from('<i',raw,3)[0]
                    enqueue(member,target,stack+((member,end,'local',None,False),) if kind=='conditional_call' else stack)
                continue
            if kind in ('jump','call'):
                enqueue(member,end+struct.unpack_from('<i',raw,2)[0],stack+((member,end,'local',None,False),) if kind=='call' else stack)
                continue
            if kind == 'dispatch':
                child = rec['dispatched_entry']
                if (child['member'],child['offset']) not in serial_children:
                    failures.append({'code':'PROJECTED_CHILD_ORDER_UNPROVED','scenario':name,
                                     'member':member,'offset':offset})
                    continue
                contracts_used[(member,offset)] = {'member':member,'offset':offset,'opcode':op,
                    'contract':'atomic_child_release_end','remaining_condition':None}
                frame = (member,end,'child',compare,False)
                compare = None  # 0203fd18 does not initialize comparison byte +2
                enqueue(child['member'],child['offset'],stack+(frame,)); continue
            if op == 0x11:
                left,right = value(vals[0]),vals[1]
                compare = None if left is None else 0 if left<right else 1 if left==right else 2
            elif op == 0x20:
                compare = None  # save flags are never inferred from the supplied save
            elif op == 0x29: variables[vals[0]] = vals[1]
            elif op == 0x2a: variables[vals[0]] = value(vals[1])
            elif op == 0x82:
                item = value(vals[0])
                result = audit.get('item_runtime',{}).get('items',{}).get(str(item))
                variables[vals[-1]] = result['pocket'] if result else None
                if result:
                    contracts_used[(member,offset)] = {'member':member,'offset':offset,'opcode':op,
                        'contract':'native_item_pocket','item':item,'pocket':result['pocket'],
                        'remaining_condition':'item loader succeeds; overlay 129 is resident; no other writer changes the operand'}
                else:
                    assumptions.add((member,offset,op,'item operand/result unresolved'))
            elif op == 0x182:
                variables[vals[0]] = initial_facing
                assumptions.add((member,offset,op,'interaction facing assumed unchanged until queried'))
            elif op in (0x69,0x6a):
                actor,outs = (255,vals) if op==0x69 else (value(vals[0]),vals[1:])
                if actor in moving:
                    failures.append({'code':'PROJECTED_READ_DURING_MOVEMENT','scenario':name,'member':member,'offset':offset})
                coords = actors.get(actor,(None,None))
                variables.update(zip(outs,coords))
            elif op == 0x153:
                actor,x,_,z,_ = map(value,vals)
                if actor is None or x is None or z is None:
                    failures.append({'code':'PROJECTED_POSITION_UNKNOWN','scenario':name,'member':member,'offset':offset})
                    continue
                actors[actor] = (x,z)
                moving.pop(actor,None)  # native setter resets held movement
            elif op in COMMAND_CONTRACTS:
                contract,effect,condition = COMMAND_CONTRACTS[op]
                contracts_used[(member,offset)] = {'member':member,'offset':offset,'opcode':op,
                    'contract':contract,'effect':effect,'remaining_condition':condition}
                if op == 0x2d9:
                    variables[vals[0]] = None  # freshly read gate, never stale output
                elif op == 0x25c:
                    variables[-1] = vals[0]  # normal follower type, if gate permits
                elif op == 0x25a:
                    variables[-2] = bool(vals[0])  # 0x40 only; not presence/visibility
            elif kind == 'child_release':
                # Only reached through the verified child contract above.
                if not stack or stack[-1][2] != 'child' or stack[-1][4]:
                    failures.append({'code':'PROJECTED_RELEASE_WITHOUT_CHILD','scenario':name,
                                     'member':member,'offset':offset})
                    continue
                stack = stack[:-1] + (stack[-1][:4] + (True,),)
                contracts_used[(member,offset)] = {'member':member,'offset':offset,'opcode':op,
                    'contract':'atomic_child_release_end','remaining_condition':None}
                enqueue(member,end,stack); continue
            elif kind == 'movement':
                actor = value(vals[0]); target = end+struct.unpack_from('<i',raw,4)[0]
                if actor in (0xf1,0xf2): actor = None  # native lookup, never a literal ID
                stream = by_stream.get((member,target))
                if stream is None:
                    failures.append({'code':'PROJECTED_STREAM_UNRESOLVED','scenario':name,
                                     'member':member,'offset':offset,'stream_offset':target})
                    continue
                if actor not in actors or actor in moving:
                    failures.append({'code':'PROJECTED_ACTOR_STATE_UNKNOWN','scenario':name,'member':member,
                                     'offset':offset,'actor':actor}); continue
                x,z = actors[actor]
                cells = [(x+dx,z+dz) for dx,dz in stream['relative_swept_tiles']]
                moving[actor] = cells[-1]
                launch = {'scenario':name,'member':member,'command_offset':offset,'stream_offset':target,
                          'actor':actor,'start':[x,z],'end':list(cells[-1]),'swept_tiles':[list(c) for c in cells],
                          'pending_actors':sorted(moving),'dynamic_qualified':False,
                          'conditional_follower_type':variables.get(-1),
                          'conditional_follower_paused':variables.get(-2)}
                launches[(name,member,offset,actor,x,z)] = launch
            elif kind == 'wait_movement':
                actors.update(moving); moving.clear()
            else:
                assumptions.add((member,offset,op,'effect not modeled in coordinate projection'))
                # Native writes into script variables must not leave stale values.
                out_last = {0x126,0x1e4,0x7f,0x7d,0x81,0x82,0x1b6,0xce,0xdc,0x182,0x2d9,0x26a,0x2ec}
                if op in out_last and vals: variables[vals[-1]] = None
                if op in (0x3c,0x3b):
                    variables[struct.unpack_from('<H',raw,len(raw)-2)[0]] = None
            enqueue(member,end)
    return {'scope':'Conditional coordinate projections for every event script, all trigger tiles, '
                    'NPC interaction positions/ranges and background cardinal approaches; not a dynamic route bound.',
            'dynamic_qualified':False,'scenarios':len(scenarios),'states_visited':len(visited),
            'launches':list(launches.values()),'endpoints':endpoints,'refusals':failures,
            'child_serialization':child_contract,'contracts_used':list(contracts_used.values()),
            'assumptions':[{'member':m,'offset':o,'opcode':op,'reason':why} for m,o,op,why in sorted(assumptions)]}
