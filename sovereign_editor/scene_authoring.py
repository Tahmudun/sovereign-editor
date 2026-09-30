"""Qualification and native persistence for bounded authored scenes."""
import struct
from .formats import require,baseline_digest,digest,resource,file_span,flat_height_plates,EditorError
from . import world, dialogue_format as fmt, event_authoring as ev, scenery
from .scene_commands import Code,emit_visibility,direction_toward

# Persistent flags pret leaves unidentified between hidden items and trainer
# flags, absent from every baseline script (any byte offset) and object event.
# The earlier 0xB47.. choice sat in the daily range (0xAA0-0xB5F), which the
# game clears at each day change.
# PROD-CAP-001 appends 18 strict event-region flags (storage.py); the first 14
# keep their order, so every recorded allocation replays unchanged.
from .storage import HIDE_FLAGS, HIDE_FLAGS_V1, EVENT_REGION
PERSISTENT_FLAG_RANGES=((0x480,0x54f),EVENT_REGION)
from .npc_behavior import WALKABLE
EXTRA_FIELDS={'presence','trigger','stock_sprite'}
# Stock overworld Pokémon qualified for scene actors: pinned-baseline readback of
# overlay131's property table and the referenced a/0/8/1 texture, all eight frames
# reviewed (evidence/scyther-event-repair-1). pret's SPRITE_FOLLOWER_MON_SCYTHER
# (552) is not Scyther in this ROM: its row selects member 384, a Seel.
STOCK_SPRITES={597:{'name':'Scyther','member':421,
                    'sha256':'632ad02a519e302a7d19ac1741c35f1d9a05d2bdb7648136bfa0690323187257'}}
LEGACY_STOCK_SPRITE=552   # story v1 history only: replay keeps its recorded meaning
# Frame-table conditions compare a variable with a literal; pret FieldSystem_VarGet
# reads IDs from VAR_BASE up as variables, so authored stage values stay below it.
VAR_BASE=0x4000


def floor_height(project,context,position):
    x,z=position['x'],position['z']
    c=project.context(header=context['header']['id'],cell=[x//32,z//32])
    point=(x-c['origin'][0]-16,z-c['origin'][1]-16)
    raw=project.member_raw(c['map_member'])
    composed=getattr(project,'_terrain_bdhc',{}).get(c['map_member'])
    if composed is not None:
        # Terrain features (terraces, carved caves) replaced this member's height table.
        from .formats import map_sections
        raw=raw[:map_sections(raw)['terrain_offset']]+composed
        raw=raw[:12]+struct.pack('<I',len(composed))+raw[16:]
    heights={p['height'] for p in flat_height_plates(raw,point=point)}
    require(len(heights)==1,'Scene tile needs one verified flat height','UNSUPPORTED_HEIGHT')
    return next(iter(heights))


def condition(value,variables):
    require(isinstance(value,dict) and set(value)=={'state','values'} and value['state'] in variables,'Choose a presence state','INVALID_EVENT')
    require(isinstance(value['values'],list) and 1<=len(value['values'])<=16 and len(set(value['values']))==len(value['values'])
            and all(type(v) is int and 0<=v<=65535 for v in value['values']),'Presence needs distinct state values','INVALID_EVENT')
    require(not variables[value['state']].get('switch') or set(value['values'])<={0,1},
            'An on/off state takes 0 (off) or 1 (on)','INVALID_EVENT')


def qualify_flags(project):
    if getattr(project,'_scene_flags_qualified',False):return
    from .character_runtime import BASELINE
    import ndspy.narc
    require(baseline_digest(project)==BASELINE,'Scene flags require pinned baseline','UNSUPPORTED_RUNTIME')
    require(all(any(lo<=f<=hi for lo,hi in PERSISTENT_FLAG_RANGES) for f in HIDE_FLAGS),'Scene flag is outside the persistent range','STATE_CONFLICT')
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


def sprite_row(project,tag):
    from .character_runtime import overlay
    o=overlay(project.blob,131)['data']
    return next((r for r in struct.iter_unpack('<3H',o[0x2260:0x2260+9900]) if r[0]==tag),None)


def qualify_stock_sprite(project,tag,version):
    if version<2:
        # The v1 rule, unchanged, so recorded transactions replay as planned.
        require(tag==LEGACY_STOCK_SPRITE,'Supported stock scene Pokémon: Scyther','INVALID_CHARACTER')
        row=sprite_row(project,tag)
        require(row is not None,'Scyther overworld property is absent','UNSUPPORTED_RUNTIME')
        require(resource(project.blob,'a/0/8/1',row[1])[1][:4]==b'BTX0','Scyther texture format differs','INVALID_CHARACTER')
        return
    spec=STOCK_SPRITES.get(tag)
    from . import npc_behavior
    if spec is None and tag in npc_behavior.APPEARANCES:
        # Stock human appearances already qualified for simple NPCs: present in
        # this ROM's own event records, never eligible for Poké Ball collection.
        require(any(e['sprite']==tag for e in npc_behavior.palette(project)),'This human appearance is absent from the ROM','INVALID_CHARACTER')
        return
    require(spec is not None,'Supported stock scene Pokémon: '+', '.join(f"{v['name']} ({k})" for k,v in STOCK_SPRITES.items()),'INVALID_CHARACTER')
    row=sprite_row(project,tag)
    require(row is not None and row[1]==spec['member'],f"{spec['name']} overworld binding differs",'UNSUPPORTED_RUNTIME')
    raw=resource(project.blob,'a/0/8/1',row[1])[1]
    require(raw[:4]==b'BTX0' and digest(raw)==spec['sha256'],f"{spec['name']} texture differs from the reviewed graphics",'INVALID_CHARACTER')


def level_script_users(project,script):
    """Headers (in ID order) whose map-load table is ``script``; one index per
    world signature (stock ARM9 rebinding plus created header records)."""
    signature=project.world_signature()
    cache=project.__dict__.get('_level_script_users')
    if cache is None or cache[0]!=signature:
        table={}
        for h in range(project.header_count()):
            table.setdefault(project.header(h)['level_script'],[]).append(h)
        cache=project._level_script_users=(signature,table)
    return list(cache[1].get(script,[]))


def extend_plan(project,context,state,index,value,after,before,version=1):
    variables=state.get('story',{}).get('state',{})
    if value.get('presence'):
        require(value['kind']=='npc','Only NPCs have presence conditions','INVALID_EVENT')
        require(value['movement']==0,'Stage-owned scene actors must stand still between scenes','INVALID_EVENT')
        condition(value['presence'],variables);qualify_flags(project)
        after['hide_flag']=allocate_hide_flag(state,before)
    if value['kind']=='entry':
        # Runs from the map's frame table once entry has finished (after init
        # scripts, object and follower spawn), before any player input.
        t=value.get('trigger')
        require(not value['once_state'],'Entry scenes use stage state, not one-time state','INVALID_EVENT')
        require(isinstance(t,dict) and set(t)=={'state','value','advance'} and t['state'] in variables,'An entry scene needs a stage, its value and the value it advances to','INVALID_EVENT')
        require(not variables[t['state']].get('switch'),'Entry stages need a number state (the frame table compares a variable)','INVALID_EVENT')
        require(all(type(t[k]) is int and 0<=t[k]<VAR_BASE for k in ('value','advance')) and t['value']!=t['advance'],
                f'Entry stages use two distinct values 0..{VAR_BASE-1}','INVALID_EVENT')
    elif value.get('trigger'):
        t=value['trigger']
        require(value['kind']=='trigger' and not value['once_state'],'Conditional triggers use stage state, not one-time state','INVALID_EVENT')
        require(isinstance(t,dict) and set(t)=={'state','value','width','height'} and t['state'] in variables,'Invalid trigger condition','INVALID_EVENT')
        require(not variables[t['state']].get('switch'),'Conditional triggers need a number state (the record compares a variable)','INVALID_EVENT')
        require(type(t['value']) is int and 0<=t['value']<=65535 and all(type(t[k]) is int and 1<=t[k]<=9 for k in ('width','height')),'Trigger extent supports1..9 tiles','INVALID_EVENT')
        require(t['width']*t['height']<=64,'Trigger exceeds64 tiles','RESOURCE_CAPACITY')
    if value.get('stock_sprite') is not None:
        require(value['kind'] in ('npc','trainer') and value['character'] is None,'Stock Pokémon appearances are for scene NPCs','INVALID_CHARACTER')
        qualify_stock_sprite(project,value['stock_sprite'],version)
    if any(k in value for k in EXTRA_FIELDS):
        # New records add dependencies without changing historical transactions.
        hm=context['header']['level_script']
        users=level_script_users(project,hm)
        require(users==[context['header']['id']],'Scene initialization requires a private map-load table','SHARED_RESOURCE')
        return {'level_script':digest(project.resource(fmt.SCRIPT_ARCHIVE,context['header']['level_script'])[1])}
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
        if not s.get('trigger') or s['kind']=='entry':continue   # entry scenes have no tiles
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


class Anywhere:
    """Tiles the player or follower may occupy when the scene cannot know: any tile."""
    def __contains__(self,p):return True
    def __repr__(self):return 'anywhere'
ANYWHERE=Anywhere()


def neighbours(p):
    x,z=p;return {(x-1,z),(x+1,z),(x,z-1),(x,z+1)}


def validate_routes(project,state,s,version=1,trace=None):
    """Follow each acyclic branch with actor positions and known state values.

    Stock roaming ranges stay occupied conservatively. Stage changes take effect
    on live objects only at sync; a later route must start where the prior one ends.
    Version 2 also tracks the following Pokémon: it trails scripted player travel
    and stays put otherwise, so other actors' routes and shown actors avoid it.
    An entry scene may start with the player anywhere (e.g. a save loaded in the
    room) until a gather confirms the arrival tile.
    """
    # An entry scene reruns every frame until it advances: always check its exits.
    if trace is None and s['kind']!='entry' and not any(n['op'] in ('move','gather','face','look','pose','reaction','collect','sync') for n in s['nodes']):return
    actors=actors_for(state,s['event_member']);nodes={n['id']:n for n in s['nodes']}
    stock=set().union(*(footprint(r) for r in scene_rows(project,state,s)))
    entry=s['kind']=='entry';follows=version>=2
    terrain={}
    def height(p):
        if p not in terrain:
            x,z=p;c=project.context(header=s['context']['header'],cell=[x//32,z//32])
            off=world.cell_offset(c,x,z);raw=project.member_raw(c['map_member'])
            pair=state['permissions'].get((c['map_member'],off),raw[off:off+2])
            require(not world.is_blocked(pair) and pair[0] in WALKABLE,f'Scene route crosses blocked or special terrain at {x},{z}','BLOCKED_TILE')
            terrain[p]=floor_height(project,c,{'x':x,'z':z})
        return terrain[p]
    def standable(p):
        try:height(p);return True
        except EditorError:return False
    def available(a,values):
        p=a.get('presence')
        return not p or p['state'] not in values or values[p['state']] in p['values']
    def definitely_present(a,values):
        p=a.get('presence')
        return not p or (p['state'] in values and values[p['state']] in p['values'])
    def tiles(t):return 'anywhere' if t is ANYWHERE else tuple(sorted(t))
    routes=gather_routes(project,state,s);initial=s.get('trigger') or s.get('presence')
    seeds=[{initial['state']:v} for v in initial.get('values',[initial.get('value')])] if initial else [{}]
    start_positions={k:(a['x'],a['z']) for k,a in actors.items()}
    def trailing(p,values):
        # Where the follower can stand after a step onto p: a free neighbour.
        taken=stock|{start_positions[k] for k,a in actors.items() if available(a,values)}
        return {q for q in neighbours(p) if q not in taken and standable(q)}
    seen=set()
    def visit(label,values,positions,player,live,follower,facings):
        signature=(label,tuple(sorted(values.items())),tuple(sorted(positions.items())),tiles(player),tuple(sorted(live)),tiles(follower),tuple(sorted((k,tuple(sorted(v))) for k,v in facings.items())))
        if signature in seen:return
        seen.add(signature);require(len(seen)<=4096,'Scene branch validation exceeds capacity','RESOURCE_CAPACITY')
        n=nodes[label];op=n['op'];values=dict(values);positions=dict(positions);live=set(live);facings={k:set(v) for k,v in facings.items()}
        if trace is not None:
            trace.append({'step':label,'op':op,'state':dict(values),'player':tiles(player),'follower':tiles(follower),
                          'actors':{k:{'tile':list(p),'facing':sorted(facings[k]),'visible':k in live} for k,p in positions.items()},
                          'player_facing':sorted(facings['player'])})
        def blocked(exclude=None):
            # The player may walk through the follower: they swap, as in free roaming.
            if exclude!='player' and ANYWHERE in (player,follower):return ANYWHERE
            occupied=stock | (player|follower if exclude!='player' else set())
            for key,a in actors.items():
                if key!=exclude and key in live:
                    x,z=positions[key];occupied|=footprint({**a,'x':x,'z':z})
            return occupied
        targets=None
        if op in ('move','face','look','reaction','collect'):
            key=n['actor'];require(key=='player' or key in actors,'Scene actor is not in this area','INVALID_EVENT')
            require(key=='player' or key in live and definitely_present(actors[key],values),'Scene uses an actor that may be absent','INVALID_EVENT')
            if op=='collect':
                require(key!='player' and 364<=actors[key].get('stock_sprite',0)<=993,'Collection needs a supported Pokémon sprite','INVALID_EVENT')
            if op=='move':
                cells=route_cells(n['path']);start=cells[0]
                require((player=={start}) if key=='player' else positions[key]==start,
                        f'Scene route {label} starts at an unverified actor position','INVALID_EVENT')
                require(ANYWHERE not in (player,follower),f'Scene route {label} needs a known player position: gather first','INVALID_EVENT')
                obstacles=blocked(key)
                for p in cells:
                    require(key=='player' or p not in follower,f'Scene route {label} crosses a tile the following Pokémon may occupy at {p}','EVENT_CONFLICT')
                    require(p not in obstacles,f'Scene route {label} crosses an actor or entrance at {p}','EVENT_CONFLICT')
                require(len({height(p) for p in cells})==1,'Scene routes require a flat verified path','UNSUPPORTED_HEIGHT')
                if key=='player':
                    player={cells[-1]}
                    if follows and len(cells)>1:follower={cells[-2]}
                else:positions[key]=cells[-1]
                if len(cells)>1:facings[key]={direction_toward(cells[-2],cells[-1],'x')}
            elif op=='face':facings[key]={n['direction']}
            elif op=='reaction' and n['reaction']=='happy':facings[key]={1}
            elif op=='look':
                target=n['target'];require(target=='player' or target in live and definitely_present(actors[target],values),'Gaze target may be absent','INVALID_EVENT')
                origins=player if key=='player' else {positions[key]}
                destinations=player if target=='player' else {positions[target]}
                require(origins is not ANYWHERE and destinations is not ANYWHERE,'Gaze needs known positions: gather first','INVALID_EVENT')
                facings[key]={direction_toward(a,b,n['axis']) for a in origins for b in destinations}
        elif op=='pose':
            for key,expected in n['actors'].items():
                require(key=='player' or key in actors,'Unknown checkpoint actor','INVALID_EVENT')
                present=key=='player' or key in live
                require(expected.get('visible',present)==present,f'Pose {label}: {key} visibility differs','POSE_MISMATCH')
                if 'tile' in expected or 'facing' in expected:require(present,f'Pose {label}: {key} is absent','POSE_MISMATCH')
                actual=player if key=='player' else {positions[key]}
                if 'tile' in expected:require(actual=={tuple(expected['tile'])},f'Pose {label}: {key} tile differs','POSE_MISMATCH')
                if 'facing' in expected:require(facings[key]=={expected['facing']},f'Pose {label}: {key} facing differs','POSE_MISMATCH')
        elif op=='gather':
            # Precomputed routes describe entry coordinates, so gather must run
            # before any actor changes, movement or prior player gathering.
            require(positions==start_positions and values in seeds,
                    'Gather must precede actor movement and state changes','INVALID_EVENT')
            approaches=routes[n['id']]
            require(player==set(approaches) or entry and player is ANYWHERE,'Gather must use the original trigger approaches','INVALID_EVENT')
            ends=set()
            for p,path in approaches.items():
                ends|={path[-2]} if len(path)>1 else ({p} if entry else trailing(p,values))
            gaze=set().union(*(facings['player'] if len(path)==1 else {direction_toward(path[-2],path[-1],'x')} for path in approaches.values()))
            visit(n['next'],values,positions,{tuple(n['destination'])},live,ends if follows else set(),{**facings,'player':gaze})
            # Unmatched: the player stands somewhere else, so nobody may move or appear.
            targets=[(n['no'],ANYWHERE,ANYWHERE if follows else set())] if 'no' in n else []
        elif op=='set':values[n['state']]=n['value']
        elif op=='sync':
            desired={k for k,a in actors.items() if available(a,values)};shown=desired-live
            for key in shown:
                positions[key]=(actors[key]['x'],actors[key]['z']);facings[key]={actors[key].get('facing',1)}
            live=desired
            for key in live:
                if ANYWHERE in (player,follower):
                    # Actors that stood still cannot be under the player; new ones might.
                    require(key not in shown,f'Shown actor {key} needs a known player position: gather first','INVALID_EVENT')
                    continue
                require(positions[key] not in follower,f'Shown actor {key} may overlap the following Pokémon','EVENT_CONFLICT')
                require(positions[key] not in blocked(key),f'Shown actor {key} overlaps an actor, player or entrance','EVENT_CONFLICT')
        elif op=='end':
            t=s.get('trigger')
            require(not t or values.get(t['state'])!=t['value'],'Conditional scene must advance its trigger state on every exit','INVALID_EVENT')
            require(not entry or values.get(t['state'])==t['advance'],'Entry scene must end at its advance stage on every exit','INVALID_EVENT')
            return
        if targets is None:
            if op=='if' and n['state'] in values:
                targets=[n['yes'] if values[n['state']]==n['value'] else n['no']]
            elif op=='if':
                yes=dict(values);yes[n['state']]=n['value'];visit(n['yes'],yes,positions,player,live,follower,facings)
                targets=[n['no']]
            else:targets=[n[k] for k in ('next','yes','no','won','lost') if k in n]
            targets=[(t,player,follower) for t in targets]
        for target,p,f in targets:visit(target,values,positions,p,live,f,facings)
    for values in seeds:
        live={k for k,a in actors.items() if available(a,values)}
        if entry:player,follower=ANYWHERE,ANYWHERE if follows else set()
        elif s.get('trigger'):
            player=set(next(iter(routes.values()))) if routes else footprint(s)
            follower=set().union(*(trailing(p,values) for p in player)) if follows else set()
        else:
            x,z=s['x'],s['z'];player=set(next(iter(routes.values()))) if routes else {(x-1,z),(x+1,z),(x,z-1),(x,z+1)}-stock
            follower=set().union(*(trailing(p,values) for p in player))-{(x,z)} if follows else set()
        facings={k:{a.get('facing',1)} for k,a in actors.items()};facings['player']={0,1,2,3}
        if s['kind']=='npc':
            own=next((k for k,a in actors.items() if a['npc_id']==s['npc_id']),None)
            if own:
                facings[own]={direction_toward(start_positions[own],p,'x') for p in player}
                facings['player']={(d^1) for d in facings[own]}
        visit(s['nodes'][0]['id'],values,start_positions,player,live,follower,facings)


def gather_routes(project,state,s):
    """Precompute bounded paths for every walkable approach tile. No runtime AI."""
    from collections import deque
    result={};actors=actors_for(state,s['event_member'])
    for n in s['nodes']:
        if n['op']!='gather':continue
        t=s.get('trigger');require(t is not None or s['kind']=='npc','Player gather needs a trigger or NPC interaction','INVALID_EVENT')
        # An entry scene's only approach is its arrival tile: the door mat the
        # player (and, per FollowMon_InitMapObject, the follower) spawns on.
        entry=s['kind']=='entry';arrival=(s['x'],s['z']) if entry else None
        talk=s['kind']=='npc'
        w,h=(3,3) if talk else (1,1) if entry else (t['width'],t['height'])
        target=tuple(n['destination']);x0=s['x']-int(talk);z0=s['z']-int(talk);x1=x0+w-1;z1=z0+h-1
        xmin,xmax=min(x0,target[0])-3,max(x1,target[0])+3
        zmin,zmax=min(z0,target[1])-3,max(z1,target[1])+3
        blocked=set().union(*(footprint(a) for a in actors.values() if not disjoint(s,a)))
        blocked|=set().union(*(footprint(r) for r in scene_rows(project,state,s)))
        blocked.discard(arrival)
        ctx=project.context(header=s['context']['header'],cell=s['context']['cell'])
        height=floor_height(project,ctx,{'x':target[0],'z':target[1]})
        def walkable(p):
            x,z=p
            if not (xmin<=x<=xmax and zmin<=z<=zmax) or p in blocked:return False
            try:
                c=project.context(header=s['context']['header'],cell=[x//32,z//32]);off=world.cell_offset(c,x,z);raw=project.member_raw(c['map_member']);pair=state['permissions'].get((c['map_member'],off),raw[off:off+2])
                return not world.is_blocked(pair) and (pair[0] in WALKABLE or p==arrival) and floor_height(project,c,{'x':x,'z':z})==height
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
                if talk and abs(x-s['x'])+abs(z-s['z'])!=1:continue
                if not walkable(p):continue
                require(p in parents,'A trigger approach cannot reach the staging tile','UNREACHABLE_ROUTE')
                path=[];q=p
                while q is not None:path.append(q);q=parents[q]
                require(len(path)<=65,'Player approach route too long','RESOURCE_CAPACITY');routes[p]=path
        require(bool(routes),'Trigger has no walkable approaches','BLOCKED_TILE');result[n['id']]=routes
    return result


FRAME_TABLE,TRANSITION=1,2   # pret INIT_SCRIPT_ON_FRAME_TABLE, INIT_SCRIPT_ON_TRANSITION


def init_records(init):
    records=[];at=0
    while at<len(init) and init[at]:
        require(at+5<=len(init) and init[at] in (1,2,3,4),'Unsupported map init table','UNSUPPORTED_SCRIPT')
        records.append((at,init[at],struct.unpack_from('<I',init,at+1)[0]));at+=5
    require(at<len(init),'Unterminated map init table','UNSUPPORTED_SCRIPT')
    return records


def add_frame_rows(init,rows):
    """Add (variable, value, script) rows to the map's per-frame condition table.

    pret e97c7fc GetMapSceneScriptId: before each frame of input processing, once
    every other init script has run, the first type-1 record's table starts the
    first row whose variable equals its value. Stock rows stay first; the combined
    table is appended, so every original byte and offset is kept.
    """
    require(rows and all(0x4000<=v<0x8000 and 0<=value<VAR_BASE and 1<=script<1000 for v,value,script in rows),
            'Invalid entry scene condition','UNSUPPORTED_SCRIPT')
    records=init_records(init);table=next(((at,ofs) for at,t,ofs in records if t==FRAME_TABLE),None)
    old=[]
    if table:
        loc,ofs=table;cursor=loc+5+ofs
        while True:
            require(0<=cursor and cursor+2<=len(init),'Unterminated map frame table','UNSUPPORTED_SCRIPT')
            if struct.unpack_from('<H',init,cursor)[0]==0:break
            require(cursor+6<=len(init) and len(old)<64,'Unsupported map frame table','UNSUPPORTED_SCRIPT')
            old.append(init[cursor:cursor+6]);cursor+=6
        patched=bytearray(init)
    else:
        # Records hold script IDs or self-relative offsets; a prepended record moves
        # every record and table together.
        loc=0;patched=bytearray(struct.pack('<BI',FRAME_TABLE,0)+init)
    at=len(patched)
    patched+=b''.join(old)+b''.join(struct.pack('<3H',*r) for r in rows)+b'\0\0'
    patched+=b'\0'*(-len(patched)%4)
    struct.pack_into('<I',patched,loc+1,at-loc-5)
    return bytes(patched)


def chain_transition(init,raw,emit):
    """Run emit's bytecode first on every map entry; keep all original records/bodies.

    pret e97c7fc runs ON_TRANSITION before Field_InitMapObjectsFromZoneEventData on
    warps (field_warp_tasks.c sub_02053038) and connections (fieldmap.c
    FieldMap_ChangeZone). ON_LOAD runs after objects exist and never on a
    connection, so stage hide flags set there would not decide who spawns.
    """
    records=init_records(init)
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
    """Stage-owned actor visibility and entry scenes on map entry. Live stage changes use sync steps."""
    from .story_authoring import allocation
    sequences=state.get('story',{}).get('sequence',{})
    areas={s['context']['header']:s for s in sequences.values() if s.get('presence')}
    entries={}
    for key,s in sequences.items():
        if s['kind']=='entry':entries.setdefault(s['context']['header'],[]).append((key,s))
    ids=allocation(project,state) if entries else {}
    for header in list(areas)+[h for h in entries if h not in areas]:
        s=areas.get(header) or entries[header][0][1]
        ctx=project.context(header=header,cell=s['context']['cell']);hm=ctx['header']['level_script'];sm=s['script_member']
        patched=project.resource(fmt.SCRIPT_ARCHIVE,hm)[1];variables=state['story']['state']
        if header in areas:
            raw=result[fmt.SCRIPT_ARCHIVE].get(sm,project.resource(fmt.SCRIPT_ARCHIVE,sm)[1])
            actors=actors_for(state,s['event_member'])
            patched,combined=chain_transition(patched,raw,lambda c:emit_visibility(c,actors,variables))
            result[fmt.SCRIPT_ARCHIVE][sm]=combined
        if header in entries:
            patched=add_frame_rows(patched,[(variables[e['trigger']['state']]['variable'],e['trigger']['value'],ids[k][0])
                                            for k,e in entries[header]])
        require(hm not in result[fmt.SCRIPT_ARCHIVE],'Conflicting map initialization edit','SHARED_RESOURCE')
        result[fmt.SCRIPT_ARCHIVE][hm]=patched
