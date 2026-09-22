"""Qualification and native persistence for bounded authored scenes."""
import struct
from .formats import require,digest,resource,file_span,flat_height_plates,EditorError
from . import world, dialogue_format as fmt, event_authoring as ev, scenery
from .scene_commands import Code,emit_visibility

# Persistent flags pret leaves unidentified between hidden items and trainer
# flags, absent from every baseline script (any byte offset) and object event.
# The earlier 0xB47.. choice sat in the daily range (0xAA0-0xB5F), which the
# game clears at each day change.
HIDE_FLAGS=(0x521,0x525,0x527,0x529,0x531,0x533,0x534,0x539,0x53b,0x53d,0x543,0x545,0x54c,0x54d)
PERSISTENT_FLAG_RANGE=(0x480,0x54f)
EXTRA_FIELDS={'presence','trigger','stock_sprite'}


def floor_height(project,context,position):
    x,z=position['x'],position['z']
    c=project.context(header=context['header']['id'],cell=[x//32,z//32])
    point=(x-c['origin'][0]-16,z-c['origin'][1]-16)
    heights={p['height'] for p in flat_height_plates(project.member_raw(c['map_member']),point=point)}
    require(len(heights)==1,'Scene tile needs one verified flat height','UNSUPPORTED_HEIGHT')
    return next(iter(heights))


def condition(value,variables):
    require(isinstance(value,dict) and set(value)=={'state','values'} and value['state'] in variables,'Choose a presence state','INVALID_EVENT')
    require(isinstance(value['values'],list) and 1<=len(value['values'])<=16 and len(set(value['values']))==len(value['values'])
            and all(type(v) is int and 0<=v<=65535 for v in value['values']),'Presence needs distinct state values','INVALID_EVENT')


def qualify_flags(project):
    if getattr(project,'_scene_flags_qualified',False):return
    from .character_runtime import BASELINE
    import ndspy.narc
    require(digest(project.blob)==BASELINE,'Scene flags require pinned baseline','UNSUPPORTED_RUNTIME')
    lo,hi=PERSISTENT_FLAG_RANGE
    require(all(lo<=f<=hi for f in HIDE_FLAGS),'Scene flag is outside the persistent range','STATE_CONFLICT')
    scripts=ndspy.narc.NARC(file_span(project.blob,fmt.SCRIPT_ARCHIVE)[1]).files
    require(all(not any(struct.pack('<H',f) in raw for raw in scripts) for f in HIDE_FLAGS),'Scene flag appears in baseline scripts','STATE_CONFLICT')
    events=ndspy.narc.NARC(file_span(project.blob,world.EVENT_ARCHIVE)[1]).files
    for raw in events:
        at=4+struct.unpack_from('<I',raw)[0]*20;n=struct.unpack_from('<I',raw,at)[0];at+=4
        require(all(struct.unpack_from('<H',raw,at+32*i+8)[0] not in HIDE_FLAGS for i in range(n)),'Scene flag belongs to a stock actor','STATE_CONFLICT')
    project._scene_flags_qualified=True


def allocate_hide_flag(state,before):
    """Keep an actor's flag; otherwise the first flag no composed sequence holds.

    Derived only from the state being planned or replayed, never project.doc:
    Redo validates a snapshot while the document still holds the undone history.
    """
    if before and before.get('hide_flag'):return before['hide_flag']
    used={s.get('hide_flag') for s in state.get('story',{}).get('sequence',{}).values()}
    free=[f for f in HIDE_FLAGS if f not in used]
    require(bool(free),'Scene actor capacity exhausted','RESOURCE_CAPACITY')
    return free[0]


def extend_plan(project,context,state,index,value,after,before):
    variables=state.get('story',{}).get('state',{})
    if value.get('presence'):
        require(value['kind']=='npc','Only NPCs have presence conditions','INVALID_EVENT')
        require(value['movement']==0,'Stage-owned scene actors must stand still between scenes','INVALID_EVENT')
        condition(value['presence'],variables);qualify_flags(project)
        after['hide_flag']=allocate_hide_flag(state,before)
    if value.get('trigger'):
        t=value['trigger']
        require(value['kind']=='trigger' and not value['once_state'],'Conditional triggers use stage state, not one-time state','INVALID_EVENT')
        require(isinstance(t,dict) and set(t)=={'state','value','width','height'} and t['state'] in variables,'Invalid trigger condition','INVALID_EVENT')
        require(type(t['value']) is int and 0<=t['value']<=65535 and all(type(t[k]) is int and 1<=t[k]<=9 for k in ('width','height')),'Trigger extent supports1..9 tiles','INVALID_EVENT')
        require(t['width']*t['height']<=64,'Trigger exceeds64 tiles','RESOURCE_CAPACITY')
    if value.get('stock_sprite') is not None:
        # First nonhuman appearance uses its existing runtime property/gfx. Keep
        # the format boundary explicit until additional species are qualified.
        require(value['kind']=='npc' and value['character'] is None and value['stock_sprite']==552,'Supported stock scene Pokémon: Scyther','INVALID_CHARACTER')
        from .character_runtime import overlay
        o=overlay(project.blob,131)['data'];table=o[0x2260:0x2260+9900]
        rows=list(struct.iter_unpack('<3H',table))
        row=next((r for r in rows if r[0]==552),None)
        require(row is not None,'Scyther overworld property is absent','UNSUPPORTED_RUNTIME')
        raw=resource(project.blob,'a/0/8/1',row[1])[1]
        require(raw[:4]==b'BTX0','Scyther texture format differs','INVALID_CHARACTER')
    if any(k in value for k in EXTRA_FIELDS):
        # New records add dependencies without changing historical transactions.
        hm=context['header']['level_script']
        users=[h for h in range(world.header_count(project.blob))
               if world.read_header(project.blob,h,project.arm9)['level_script']==hm]
        require(users==[context['header']['id']],'Scene initialization requires a private map-load table','SHARED_RESOURCE')
        return {'level_script':digest(resource(project.blob,fmt.SCRIPT_ARCHIVE,context['header']['level_script'])[1])}
    return {}


def disjoint(a,b):
    pa,pb=a.get('presence') or a.get('trigger'),b.get('presence') or b.get('trigger')
    if not pa or not pb or pa['state']!=pb['state']:return False
    va=set(pa.get('values',[pa.get('value')]));vb=set(pb.get('values',[pb.get('value')]))
    return va.isdisjoint(vb)


def actors_for(state,member):
    return {k:s for k,s in state.get('story',{}).get('sequence',{}).items() if s['event_member']==member and s['kind']=='npc'}


def footprint(r):
    x,z=r['x'],r['z'];kind=r['kind']
    if kind=='npc':
        rx,rz=max(0,r.get('range_x',0)),max(0,r.get('range_z',0))
        return {(xx,zz) for xx in range(x-rx,x+rx+1) for zz in range(z-rz,z+rz+1)}
    if kind=='trigger':
        t=r.get('trigger') or r
        return {(xx,zz) for xx in range(x,x+t.get('width',1)) for zz in range(z,z+t.get('height',1))}
    if kind=='warp':return {(x,z),(x,z+1)}
    return {(x,z)}


def scene_rows(project,state,s):
    ids={a['npc_id'] for a in actors_for(state,s['event_member']).values()}
    return [r for r in ev.records(ev.raw_member(project,s['event_member'],state))
            if r['kind'] in ('npc','warp') and not (r['kind']=='npc' and r['id'] in ids)]


def validate_triggers(project,state):
    from .story_authoring import allocation
    sequences=state.get('story',{}).get('sequence',{});ids=allocation(project,state)
    for key,s in sequences.items():
        if not s.get('trigger'):continue
        domain=footprint(s)
        owned={ids[k][0]:v for k,v in sequences.items() if v['event_member']==s['event_member'] and v['kind']=='trigger'}
        for r in ev.records(ev.raw_member(project,s['event_member'],state)):
            if r['kind'] not in ('trigger','warp'):continue
            if r['kind']=='trigger':
                if r['script']==ids[key][0]:continue
                other=owned.get(r['script'])
                if other and disjoint(s,other):continue
            require(not domain.intersection(footprint(r)),
                    f'Trigger {key} overlaps {r["kind"]} {r["id"]}','EVENT_CONFLICT')


def route_cells(path):
    cells=[tuple(path[0])]
    for (x,z),(xx,zz) in zip(path,path[1:]):
        dx=(xx>x)-(xx<x);dz=(zz>z)-(zz<z)
        while (x,z)!=(xx,zz):x+=dx;z+=dz;cells.append((x,z))
    return cells


def validate_routes(project,state,s):
    """Follow each acyclic branch with actor positions and known state values.

    Stock roaming ranges stay occupied conservatively. Stage changes take effect
    on live objects only at sync; a later route must start where the prior one ends.
    """
    if not any(n['op'] in ('move','gather','face','reaction','sync') for n in s['nodes']):return
    actors=actors_for(state,s['event_member']);nodes={n['id']:n for n in s['nodes']}
    stock=set().union(*(footprint(r) for r in scene_rows(project,state,s)))
    terrain={}
    def height(p):
        if p not in terrain:
            x,z=p;c=project.context(header=s['context']['header'],cell=[x//32,z//32])
            off=world.cell_offset(c,x,z);raw=project.member_raw(c['map_member'])
            pair=state['permissions'].get((c['map_member'],off),raw[off:off+2])
            require(not world.is_blocked(pair) and pair[0] in (0,2),f'Scene route crosses blocked or special terrain at {x},{z}','BLOCKED_TILE')
            terrain[p]=floor_height(project,c,{'x':x,'z':z})
        return terrain[p]
    def available(a,values):
        p=a.get('presence')
        return not p or p['state'] not in values or values[p['state']] in p['values']
    def definitely_present(a,values):
        p=a.get('presence')
        return not p or (p['state'] in values and values[p['state']] in p['values'])
    routes=gather_routes(project,state,s);initial=s.get('trigger') or s.get('presence')
    seeds=[{initial['state']:v} for v in initial.get('values',[initial.get('value')])] if initial else [{}]
    seen=set()
    def visit(label,values,positions,player,live):
        signature=(label,tuple(sorted(values.items())),tuple(sorted(positions.items())),tuple(sorted(player)),tuple(sorted(live)))
        if signature in seen:return
        seen.add(signature);require(len(seen)<=4096,'Scene branch validation exceeds capacity','RESOURCE_CAPACITY')
        n=nodes[label];op=n['op'];values=dict(values);positions=dict(positions);live=set(live)
        def blocked(exclude=None):
            occupied=stock | (player if exclude!='player' else set())
            for key,a in actors.items():
                if key!=exclude and key in live:
                    x,z=positions[key];occupied|=footprint({**a,'x':x,'z':z})
            return occupied
        if op in ('move','face','reaction'):
            key=n['actor'];require(key=='player' or key in actors,'Scene actor is not in this area','INVALID_EVENT')
            require(key=='player' or key in live and definitely_present(actors[key],values),'Scene uses an actor that may be absent','INVALID_EVENT')
            if op=='move':
                cells=route_cells(n['path']);start=cells[0]
                require((player=={start}) if key=='player' else positions[key]==start,
                        f'Scene route {label} starts at an unverified actor position','INVALID_EVENT')
                obstacles=blocked(key)
                for p in cells:require(p not in obstacles,f'Scene route {label} crosses an actor or entrance at {p}','EVENT_CONFLICT')
                require(len({height(p) for p in cells})==1,'Scene routes require a flat verified path','UNSUPPORTED_HEIGHT')
                if key=='player':player={cells[-1]}
                else:positions[key]=cells[-1]
        elif op=='gather':
            # Precomputed routes describe entry coordinates, so gather must run
            # before any actor changes, movement or prior player gathering.
            require(positions=={k:(a['x'],a['z']) for k,a in actors.items()} and values in seeds,
                    'Gather must precede actor movement and state changes','INVALID_EVENT')
            require(player==set(routes[n['id']]),'Gather must use the original trigger approaches','INVALID_EVENT')
            player={tuple(n['destination'])}
        elif op=='set':values[n['state']]=n['value']
        elif op=='sync':
            desired={k for k,a in actors.items() if available(a,values)}
            for key in desired-live:positions[key]=(actors[key]['x'],actors[key]['z'])
            live=desired
            for key in live:
                require(positions[key] not in blocked(key),f'Shown actor {key} overlaps an actor, player or entrance','EVENT_CONFLICT')
        elif op=='end':
            t=s.get('trigger')
            require(not t or values.get(t['state'])!=t['value'],'Conditional scene must advance its trigger state on every exit','INVALID_EVENT')
            return
        if op=='if' and n['state'] in values:
            targets=[n['yes'] if values[n['state']]==n['value'] else n['no']]
        elif op=='if':
            yes=dict(values);yes[n['state']]=n['value'];visit(n['yes'],yes,positions,player,live)
            targets=[n['no']]
        else:targets=[n[k] for k in ('next','yes','no','won','lost') if k in n]
        for target in targets:visit(target,values,positions,player,live)
    for values in seeds:
        if s.get('trigger'):
            player=set(next(iter(routes.values()))) if routes else footprint(s)
        else:
            x,z=s['x'],s['z'];player={(x-1,z),(x+1,z),(x,z-1),(x,z+1)}-stock
        visit(s['nodes'][0]['id'],values,{k:(a['x'],a['z']) for k,a in actors.items()},player,
              {k for k,a in actors.items() if available(a,values)})


def gather_routes(project,state,s):
    """Precompute bounded paths for every walkable approach tile. No runtime AI."""
    from collections import deque
    result={};actors=actors_for(state,s['event_member'])
    for n in s['nodes']:
        if n['op']!='gather':continue
        t=s.get('trigger');require(t is not None,'Player gather needs a conditional trigger','INVALID_EVENT')
        target=tuple(n['destination']);x0=s['x'];z0=s['z'];x1=x0+t['width']-1;z1=z0+t['height']-1
        xmin,xmax=min(x0,target[0])-3,max(x1,target[0])+3
        zmin,zmax=min(z0,target[1])-3,max(z1,target[1])+3
        blocked=set().union(*(footprint(a) for a in actors.values() if not disjoint(s,a)))
        blocked|=set().union(*(footprint(r) for r in scene_rows(project,state,s)))
        ctx=project.context(header=s['context']['header'],cell=s['context']['cell'])
        height=floor_height(project,ctx,{'x':target[0],'z':target[1]})
        def walkable(p):
            x,z=p
            if not (xmin<=x<=xmax and zmin<=z<=zmax) or p in blocked:return False
            try:
                c=project.context(header=s['context']['header'],cell=[x//32,z//32]);off=world.cell_offset(c,x,z);raw=project.member_raw(c['map_member']);pair=state['permissions'].get((c['map_member'],off),raw[off:off+2])
                return not world.is_blocked(pair) and pair[0] in (0,2) and floor_height(project,c,{'x':x,'z':z})==height
            except EditorError:return False
        require(walkable(target),'Player staging tile is blocked or changes height','BLOCKED_TILE')
        parents={target:None};queue=deque([target])
        while queue:
            x,z=queue.popleft()
            for p in ((x,z-1),(x,z+1),(x-1,z),(x+1,z)):
                if p not in parents and walkable(p):parents[p]=(x,z);queue.append(p)
        routes={}
        for x in range(x0,x1+1):
            for z in range(z0,z1+1):
                p=(x,z)
                if not walkable(p):continue
                require(p in parents,'A trigger approach cannot reach the staging tile','UNREACHABLE_ROUTE')
                path=[];q=p
                while q is not None:path.append(q);q=parents[q]
                require(len(path)<=65,'Player approach route too long','RESOURCE_CAPACITY');routes[p]=path
        require(bool(routes),'Trigger has no walkable approaches','BLOCKED_TILE');result[n['id']]=routes
    return result


TRANSITION=2   # pret INIT_SCRIPT_ON_TRANSITION


def chain_transition(init,raw,emit):
    """Run emit's bytecode first on every map entry; keep all original records/bodies.

    pret e97c7fc runs ON_TRANSITION before Field_InitMapObjectsFromZoneEventData on
    warps (field_warp_tasks.c sub_02053038) and connections (fieldmap.c
    FieldMap_ChangeZone). ON_LOAD runs after objects exist and never on a
    connection, so stage hide flags set there would not decide who spawns.
    """
    records=[];at=0
    while at<len(init) and init[at]:
        require(at+5<=len(init) and init[at] in (1,2,3,4),'Unsupported map init table','UNSUPPORTED_SCRIPT')
        records.append((at,init[at],struct.unpack_from('<I',init,at+1)[0]));at+=5
    require(at<len(init),'Unterminated map init table','UNSUPPORTED_SCRIPT')
    require(sum(t==TRANSITION for _,t,_ in records)<=1,'Duplicate map transition script','UNSUPPORTED_SCRIPT')
    oldentries=fmt.script_entries(raw)[1];new_id=len(oldentries)+1
    previous=next((val for _,t,val in records if t==TRANSITION),None)
    c=Code();emit(c)
    require(not c.movements,'Map transition scripts cannot move actors','UNSUPPORTED_SCRIPT')
    if previous is not None:
        require(previous>>16==0 and 1<=previous<=len(oldentries),'Unsupported map transition script ID','UNSUPPORTED_SCRIPT')
        c.emit('Hi',22,0)   # GoTo the original transition script afterwards
    else:c.emit('H',2)
    payload=c.finish();combined=bytearray(fmt.append_scripts(raw,[payload]))
    if previous is not None:
        entries=fmt.script_entries(combined)[1];jump_at=entries[-1]+len(payload)-4
        struct.pack_into('<i',combined,jump_at,entries[previous-1]-jump_at-4)
        patched=bytearray(init);loc=next(i for i,t,_ in records if t==TRANSITION);struct.pack_into('<I',patched,loc+1,new_id)
    else:
        # Frame-table records hold offsets relative to themselves; prepending a
        # fixed five-byte record moves each record and its table together.
        patched=bytearray(struct.pack('<BI',TRANSITION,new_id)+init)
    return bytes(patched),bytes(combined)


def add_initializers(project,state,result):
    """Stage-owned actor visibility on map entry. Live stage changes use sync steps."""
    areas={s['context']['header']:s for s in state.get('story',{}).get('sequence',{}).values() if s.get('presence')}
    for header,s in areas.items():
        ctx=project.context(header=header,cell=s['context']['cell']);hm=ctx['header']['level_script'];sm=s['script_member']
        init=resource(project.blob,fmt.SCRIPT_ARCHIVE,hm)[1]
        raw=result[fmt.SCRIPT_ARCHIVE].get(sm,resource(project.blob,fmt.SCRIPT_ARCHIVE,sm)[1])
        actors=actors_for(state,s['event_member']);variables=state['story']['state']
        patched,combined=chain_transition(init,raw,lambda c:emit_visibility(c,actors,variables))
        result[fmt.SCRIPT_ARCHIVE][sm]=combined
        require(hm not in result[fmt.SCRIPT_ARCHIVE],'Conflicting map initialization edit','SHARED_RESOURCE')
        result[fmt.SCRIPT_ARCHIVE][hm]=patched
