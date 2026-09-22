"""Project-owned simple NPC/sign creation with isolated scripts and text."""
import copy
import struct
from . import authoring, world, dialogue_format as fmt, scenery, npc_behavior
from .formats import digest, require, resource, EditorError

SCHEMA='sovereign-simple-interaction-v1'
BEHAVIOR_SCHEMA='sovereign-simple-interaction-v2'
SCHEMAS=(SCHEMA,BEHAVIOR_SCHEMA)


def specs(state):return state.get('simple_interactions',{})


def allocation(project,state):
    result={};sc={};tc={}
    for identity,s in specs(state).items():
        script=s['script_member'];text=s['text_member']
        if script not in sc:sc[script]=len(fmt.script_entries(resource(project.blob,fmt.SCRIPT_ARCHIVE,script)[1])[1])
        if text not in tc:tc[text]=len(fmt.text_entries(resource(project.blob,fmt.TEXT_ARCHIVE,text)[1])[1])
        result[identity]=(sc[script]+1,tc[text]);sc[script]+=1;tc[text]+=1
        require(sc[script]<1000 and tc[text]<=256,'Simple script/text capacity exceeded','RESOURCE_CAPACITY')
    return result


def record(s,script):
    if s['kind']=='npc':
        r=bytearray(32)
        struct.pack_into('<6Hh',r,0,s['npc_id'],s['sprite'],s.get('movement',0),0,0,script,s['facing'])
        struct.pack_into('<2h',r,20,s.get('range_x',0),s.get('range_z',0))
        struct.pack_into('<2Hi',r,24,s['x'],s['z'],s['y'])
        return bytes(r)
    return struct.pack('<HHiiiHH',script,0,s['x'],s['z'],s['y'],4,0)


def append_events(project,member,state,raw):
    selected=[(k,s) for k,s in specs(state).items() if s['event_member']==member]
    if not selected:return raw
    ids=allocation(project,state);cursor=0;result=bytearray()
    for kind,size in [('background',20),('npc',32),('warp',12),('trigger',16)]:
        count=struct.unpack_from('<I',raw,cursor)[0];cursor+=4
        added=[record(s,ids[k][0]) for k,s in selected if s['kind']==kind]
        result.extend(struct.pack('<I',count+len(added)));result.extend(raw[cursor:cursor+count*size]);result.extend(b''.join(added));cursor+=count*size
    require(cursor==len(raw) and len(result)<=0x800,'Event member exceeds the HGSS 0x800-byte buffer','RESOURCE_CAPACITY')
    return bytes(result)


def replacements(project,state):
    ids=allocation(project,state);texts={};scripts={}
    for key,s in specs(state).items():
        _,message=ids[key]
        texts.setdefault(s['text_member'],[]).append(s['dialogue'])
        scripts.setdefault(s['script_member'],[]).append(fmt.talk_script(message,s['kind']))
    return {fmt.TEXT_ARCHIVE:{m:fmt.append_messages(resource(project.blob,fmt.TEXT_ARCHIVE,m)[1],v) for m,v in texts.items()},
            fmt.SCRIPT_ARCHIVE:{m:fmt.append_scripts(resource(project.blob,fmt.SCRIPT_ARCHIVE,m)[1],v) for m,v in scripts.items()}}


def validate(project,state):
    from . import event_authoring as ev
    for key,s in specs(state).items():
        ctx=project.context(header=s['context']['header'],cell=s['context']['cell'])
        offset=world.cell_offset(ctx,s['x'],s['z']);base=project.member_raw(ctx['map_member'])
        pair=state['permissions'].get((ctx['map_member'],offset),base[offset:offset+2])
        if s['kind']=='npc':require(not world.is_blocked(pair),'New NPC stands on blocked terrain','BLOCKED_TILE')
        rows=ev.records(ev.raw_member(project,ctx['event_member'],state))
        if s['kind']=='npc' and 'movement' in s:
            from . import npc_behavior
            npc_behavior.validate_area(project,ctx,s,state,rows)
        for r in rows:
            if s['kind']=='npc' and r['kind']=='npc' and r['id']==s['npc_id']:continue
            if r['kind']=='npc':
                conflict=abs(r['x']-s['x'])<=max(0,r['range_x']) and abs(r['z']-s['z'])<=max(0,r['range_z'])
            elif r['kind']=='warp':conflict=r['x']==s['x'] and r['z']<=s['z']<=r['z']+1
            elif r['kind']=='trigger':conflict=r['x']<=s['x']<r['x']+r['width'] and r['z']<=s['z']<r['z']+r['height']
            else:continue
            require(not conflict,'Simple interaction overlaps an actor, entrance or trigger','EVENT_CONFLICT')
        if s['kind']=='background':
            require(sum(r['kind']=='background' and (r['x'],r['z'])==(s['x'],s['z']) for r in rows)==1,
                    'Sign overlaps another background interaction','EVENT_CONFLICT')
    replacements(project,state)


def plan(project,context,state,index,action,identity=None,kind=None,donor_id=None,x=None,z=None,facing=None,dialogue=None,label=None,
         movement=None,range_x=None,range_z=None,sprite=None,_version=2):
    from . import event_authoring as ev
    from .area_layout import resource_users
    require(action in ('create','edit','delete','duplicate'),'Unknown interaction action','INVALID_INPUT')
    if action=='delete':
        require(all(v is None for v in (movement,range_x,range_z,sprite)),
                'Removal does not accept NPC behavior changes','INVALID_INPUT')
    require(label is None or isinstance(label,str) and 0<len(label)<=160,'Invalid interaction label')
    current=copy.deepcopy(specs(state));before=current.get(identity)
    if action!='create':require(before is not None,'Only authored simple interactions can be edited or removed','BOUND_EVENT')
    if before is not None:
        require(before['context']==authoring.context_ref(context), 'Choose the authored interaction’s original context', 'CONTEXT_MISMATCH')
    users=resource_users(project,context)
    for header in users['events']:
        other=world.read_header(project.blob,header,project.arm9)
        require((other['script_file'],other['text_archive']) ==
                (context['header']['script_file'],context['header']['text_archive']),
                'Shared events use different script/text resources in another header', 'SHARED_RESOURCE')
    if action=='delete':after=None
    else:
        if action=='edit':
            s=copy.deepcopy(before)
            require(s['context']==authoring.context_ref(context),'Choose the interaction’s original context','CONTEXT_MISMATCH')
            if x is not None:s['x']=x
            if z is not None:s['z']=z
            if dialogue is not None:s['dialogue']=dialogue
            if facing is not None:s['facing']=facing
        else:
            if action=='duplicate':kind=before['kind'];donor_id=before['donor_id'];dialogue=dialogue if dialogue is not None else before['dialogue']
            require(kind in ('npc','background'),'Choose NPC or sign','INVALID_INPUT')
            donor=next((r for r in ev.records(ev.base(project,context['event_member'])) if r['kind']==kind and r['id']==donor_id),None)
            require(donor is not None,'Choose a baseline appearance/height donor in this map','NOT_FOUND')
            head=context['header'];identity=f'simple:{index}'
            npc_ids=[r['id'] for r in ev.records(ev.base(project,context['event_member'])) if r['kind']=='npc']
            npc_id=max(npc_ids,default=-1)+index+1
            require(kind!='npc' or npc_id<240,'No supported fresh NPC local ID','RESOURCE_CAPACITY')
            s={'context':authoring.context_ref(context),'event_member':context['event_member'],
               'script_member':head['script_file'],'text_member':head['text_archive'],'kind':kind,
               'donor_id':donor_id,'donor_sha256':digest(donor['raw']),'npc_id':npc_id,
               'sprite':donor.get('sprite'),'y':struct.unpack_from('<i',donor['raw'],28 if kind=='npc' else 12)[0],
               'x':x,'z':z,'facing':facing if facing is not None else (before['facing'] if before else 1),'dialogue':dialogue}
            source={'x':donor['x'],'z':donor['z']};target={'x':x,'z':z}
            world.cell_offset(context,donor['x'],donor['z'])
            world.cell_offset(context,x,z)
            require(scenery.floor_height(project,context,source)==scenery.floor_height(project,context,target),
                    'New interaction needs the donor’s verified flat ground height','UNSUPPORTED_HEIGHT')
        world.cell_offset(context,s['x'],s['z'])
        if action=='edit':
            require(scenery.floor_height(project,context,before)==scenery.floor_height(project,context,s),
                    'Interaction movement would change its verified ground height','UNSUPPORTED_HEIGHT')
        require(type(s['facing']) is int and 0<=s['facing']<=3,'Facing must be 0..3','INVALID_INPUT')
        if _version==2 and s['kind']=='npc':
            from . import npc_behavior
            for field,value in [('movement',movement),('range_x',range_x),('range_z',range_z)]:
                s[field]=value if value is not None else (before or {}).get(field,0)
            if sprite is not None:
                require(type(sprite) is int and any(e['sprite']==sprite for e in npc_behavior.palette(project)),
                        'Choose a verified stock human appearance','INVALID_INPUT')
                s['sprite']=sprite
            elif action=='duplicate':s['sprite']=before['sprite']
            npc_behavior.validate_fields(s)
            if s['movement']:
                require(any(e['sprite']==s['sprite'] for e in npc_behavior.palette(project)),
                        'Moving NPCs need a supported stock human appearance','INVALID_INPUT')
        elif _version==2:
            require(all(v is None for v in (movement,range_x,range_z,sprite)),
                    'NPC behavior fields do not apply to a sign','INVALID_INPUT')
        fmt.encode_message(s['dialogue']);after=s
        if _version==2 and action=='edit' and s['kind']=='npc':
            normalized=copy.deepcopy(before)
            for field in ('movement','range_x','range_z'):normalized.setdefault(field,0)
            if normalized==s:after=copy.deepcopy(before)
    if after is None:del current[identity]
    else:current[identity]=after
    trial={**state,'simple_interactions':current};validate(project,trial)
    deps={'event':digest(ev.base(project,context['event_member'])),
          'script':digest(resource(project.blob,fmt.SCRIPT_ARCHIVE,context['header']['script_file'])[1]),
          'text':digest(resource(project.blob,fmt.TEXT_ARCHIVE,context['header']['text_archive'])[1]),
          'context':authoring.dependencies(context,index), 'shared_headers':users}
    request=dict(action=action,identity=identity if action not in ('create','duplicate') else (None if action=='create' else next(k for k,v in specs(state).items() if v==before)),kind=kind,donor_id=donor_id,x=x,z=z,facing=facing,dialogue=dialogue,label=label)
    if _version==2:
        request.update(movement=movement,range_x=range_x,range_z=range_z,sprite=sprite)
        if sprite is not None:deps['appearance']=next(e for e in npc_behavior.palette(project) if e['sprite']==sprite)
    return {'schema':SCHEMA if _version==1 else BEHAVIOR_SCHEMA,'index':index,'context':authoring.context_ref(context),'identity':identity,
            'label':label or f'{action.capitalize()} simple {after["kind"] if after else before["kind"]}',
            'request':request,'before':before if action not in ('create','duplicate') else None,'after':after,
            'dependencies':deps,'dependencies_sha256':authoring.canonical(deps)}


def replay(project,state,t,index):
    try:
        ctx=project.context(header=t['context']['header'],cell=t['context']['cell'])
        expected=plan(project,ctx,state,index,**t['request'],_version=1 if t['schema']==SCHEMA else 2)
        require(t==expected,'Simple interaction before-value or dependency differs','BEFORE_VALUE_MISMATCH')
        state.setdefault('simple_interactions',{})
        if t['after'] is None:del state['simple_interactions'][t['identity']]
        else:state['simple_interactions'][t['identity']]=t['after']
        state['contexts'].append(ctx)
    except (KeyError,TypeError,ValueError,struct.error) as exc:raise EditorError('INVALID_INPUT','Malformed simple interaction') from exc


def summary(t):return {'operation':'simple.interaction','index':t['index'],'label':t['label'],'identity':t['identity'],'context':t['context'],'before':t['before'],'after':t['after']}
