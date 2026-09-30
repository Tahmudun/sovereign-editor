"""Explicit group/property requests and compatible stock donor browsing."""
from . import authoring,world,scenery,event_authoring,mapscene
from .formats import require,EditorError,digest


def group(project,context,slots,action,dx=0,dz=0,destination=None,cells=(),events=()):
    require(action in ('move','duplicate','delete','transfer'),'Unknown group action','INVALID_INPUT')
    require(isinstance(slots,list) and 1<=len(slots)<=32 and len(set(slots))==len(slots),
            'Choose 1..32 distinct placed objects','INVALID_INPUT')
    require(all(type(v) is int for v in (dx,dz)),'Group offsets are whole tiles','INVALID_INPUT')
    require(not events or action in ('move','transfer'),'Linked events can move with an existing property; they cannot be copied/deleted','BOUND_EVENT')
    require(not events or action=='move','Linked event transfer across cells is not supported','OUTSIDE_MAP')
    target=project.context(**destination) if destination else context
    require((action=='transfer')==(destination is not None),'Only transfer uses a destination context')
    ctx={'header':context['header']['id'],'cell':[context['cell']['x'],context['cell']['y']]}
    dest={'header':target['header']['id'],'cell':[target['cell']['x'],target['cell']['y']]}
    state=project.composed();table=scenery.table_for(project,context,state);ops=[]
    # Collision is a union over the entire group. Overlapping old/new cells are
    # evaluated once, so moving adjacent objects cannot clear each other's cells.
    source=set();targets=set();types={};linked=[];seen=set()
    for link in events:
        require(isinstance(link,dict) and set(link)=={'kind','event_id'},'Link needs event kind and ID')
        key=(link['kind'],link['event_id']);require(key not in seen,'Duplicate event link');seen.add(key)
        e=event_authoring.lookup(project,context['event_member'],*key,state);linked.append((link,e))
        if e['kind']=='warp' and (dx or dz):
            # The engine tests the metatile behavior as well as the warp record.
            # A door moved without 0x69 cannot be entered from its blocked face.
            old=(context['map_member'],e['x'],e['z']);new=(old[0],e['x']+dx,e['z']+dz)
            raw=project.member_raw(old[0]);a=world.cell_offset(context,*old[1:]);b=world.cell_offset(context,*new[1:])
            before=state['permissions'].get((old[0],a),raw[a:a+2]);after=state['permissions'].get((old[0],b),raw[b:b+2])
            require(before[0]==0x69 and before[1]&128,
                    'Linked entrance moves currently require a blocked stock door tile (0x69)', 'UNSUPPORTED_ENTRANCE')
            require(after[0]==0,'A linked door needs an ordinary destination tile type', 'UNSUPPORTED_ENTRANCE')
            require(old not in types and new not in types,'Linked doorway tiles overlap','EVENT_CONFLICT')
            source.add(old);targets.add(new);types[old]=0;types[new]=0x69
    for c in cells:
        require(isinstance(c,dict) and set(c)=={'x','z'},'Selected cells need x/z')
        offset=world.cell_offset(context,c['x'],c['z']);raw=project.member_raw(context['map_member'])
        pair=state['permissions'].get((context['map_member'],offset),raw[offset:offset+2])
        require(pair[1]&128,'Selected source collision must be blocked','BEFORE_VALUE_MISMATCH')
        source.add((context['map_member'],c['x'],c['z']))
        if action!='delete':targets.add((target['map_member'],c['x']+dx,c['z']+dz))
    affected=targets|(source if action!='duplicate' else set());contexts={context['map_member']:context,target['map_member']:target}
    for member in sorted({m for m,_,_ in affected}):
        selected=contexts[member];perms=[]
        for m,x,z in sorted(affected):
            if m!=member:continue
            offset=world.cell_offset(selected,x,z);raw=project.member_raw(m)
            pair=state['permissions'].get((m,offset),raw[offset:offset+2]);flag=128 if (m,x,z) in targets else 0
            perms.append(dict(x=x,z=z,before=pair.hex(),after=bytes([types.get((m,x,z),pair[0]),pair[1]&127|flag]).hex()))
        ops.append(dict(kind='map',context={'header':selected['header']['id'],'cell':[selected['cell']['x'],selected['cell']['y']]},request={'permissions':perms}))
    for slot in slots:
        require(type(slot) is int and slot in table,'Group object missing','NOT_FOUND')
        pos=authoring.global_from_record(context,scenery.words(table[slot]['raw']))
        request={'operation':action,'slot':slot}
        if action!='delete':
            after={**pos,'x':pos['x']+dx,'z':pos['z']+dz}
            require(scenery.floor_height(project,context,pos)==scenery.floor_height(project,target,after),
                    'Group move would change an anchor’s verified floor height','UNSUPPORTED_HEIGHT')
            request.update(x=after['x'],z=after['z'])
        if destination:request['destination']=dest
        ops.append(dict(kind='scenery',context=ctx,request=request))
    for link,e in linked:
        request={**link,'values':{'x':e['x']+dx,'z':e['z']+dz},'reciprocal':e['kind']=='warp'}
        ops.append(dict(kind='event',context=ctx,request=request))
    require(len(ops)<=64,'Group exceeds transaction operation limit')
    return ops


def library_sources(project,context):
    key=(context['resources']['building_models']['archive'],context['area_data']['buildings_tileset'])
    if not hasattr(project,'_library_source_cache'):project._library_source_cache={}
    if key not in project._library_source_cache:
        entries=[];areas={};matrices={};seen=set()
        for h in range(project.header_count()):
            head=project.header(h)
            aid=head['area_data']
            if aid not in areas:areas[aid]=world.read_area_data(project.blob,aid)
            a=areas[aid];archive=world.INTERIOR_MODEL_ARCHIVE if a['area_type']==0 else world.BUILDING_MODEL_ARCHIVE
            if (archive,a['buildings_tileset'])!=key:continue
            mid=head['matrix']
            if mid not in matrices:matrices[mid]=project.matrix_data(mid)
            grid=matrices[mid]
            for cell in world.matrix_cells(grid):
                if grid['has_headers'] and cell['header']!=h:continue
                if cell['map_member'] in seen:continue
                seen.add(cell['map_member'])
                entries.append({'header':h,'cell':cell['cell'],'map_member':cell['map_member'],
                                'name':mapscene.area_title(head['name']),'internal_name':head['name']})
        project._library_source_cache[key]=entries
    return project._library_source_cache[key]


def templates(project,context,source,search='',images=False):
    require(isinstance(search,str),'Search must be text')
    donor=project.context(**source)
    require(context['resources']['building_models']==donor['resources']['building_models'] and
            context['resources']['building_textures']==donor['resources']['building_textures'],
            'Donor assets are incompatible','INCOMPATIBLE_ASSETS')
    entries=project.map_palette(**source,images=images)['templates']
    return [e for e in entries if e['status']=='ok' and search.casefold() in (e['display']+' '+str(e['model_id'])).casefold()]


def resource_users(project,context):
    if not hasattr(project, '_resource_users_versions'):
        project._resource_users_versions = {}
    version = project.world_signature()
    if version in project._resource_users_versions:
        project._area_resource_users = project._resource_users_versions[version]
    if not hasattr(project,'_area_resource_users'):
        maps={};events={};scripts={};texts={};matrices={}
        for h in range(project.header_count()):
            head=project.header(h)
            events.setdefault(head['event_file'],[]).append(h);scripts.setdefault(head['script_file'],[]).append(h);texts.setdefault(head['text_archive'],[]).append(h)
            mid=head['matrix']
            if mid not in matrices:matrices[mid]=project.matrix_data(mid)
            grid=matrices[mid]
            for c in world.matrix_cells(grid):
                if grid['has_headers'] and c['header']!=h:continue
                maps.setdefault(c['map_member'],[]).append({'header':h,'cell':c['cell'],'name':head['name']})
        project._area_resource_users={'maps':maps,'events':events,'scripts':scripts,'texts':texts}
        project._resource_users_versions[version] = project._area_resource_users
    u=project._area_resource_users;head=context['header']
    return {'maps':u['maps'].get(context['map_member'],[]),'events':u['events'][context['event_member']],
            'scripts':u['scripts'][head['script_file']],'texts':u['texts'][head['text_archive']]}
