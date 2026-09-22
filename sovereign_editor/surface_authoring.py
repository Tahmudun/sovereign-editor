"""Deterministic surface requests rebuilt from the immutable model, never a prior export."""
import copy
import functools
import numpy as np
from . import authoring, mapscene, nitro, surface_format, scenery
from .formats import require,digest,map_data,EditorError

SCHEMA='sovereign-surface-transaction-v1'
NATIVE_SCHEMA='sovereign-surface-transaction-v2'
SAMPLED_SCHEMA='sovereign-surface-transaction-v3'
SCHEMAS=(SCHEMA,NATIVE_SCHEMA,SAMPLED_SCHEMA)


def palette(project,context):
    raw=map_data(project.member_raw(context['map_member']))[2]
    refs,blobs=mapscene.tilesets(project,context)
    summary,prims=nitro.decode_model(raw,tileset=blobs['map_tileset'])
    _,_,_,_,draws=surface_format.layout(raw)
    entries=[]
    for (sid,_,_),p in zip(draws,prims):
        name=p.material['texture_name'] or ''
        if not (name in ('grass01gs','grass02','road01') or 'yuka' in name):continue
        if not all(p.material['repeat']):continue
        heights=sorted({float(p.vertices[t[0],1]) for t in p.triangles if np.ptp(p.vertices[t,1])<1e-4})
        if heights:entries.append({'material':p.material['name'],'texture':name,'shape':sid,'heights':heights})
    return entries


@functools.lru_cache(maxsize=32)
def build(raw,tileset,assignments,version=1):
    if not assignments:return raw
    if version>=2:
        from . import surface_native
        return surface_native.build(raw,tileset,assignments)
    _,prims=nitro.decode_model(raw,tileset=tileset)
    _,_,_,_,draws=surface_format.layout(raw)
    shapes={p.material['name']:sid for (sid,_,_),p in zip(draws,prims)}
    out=raw
    for x,z,material,height in assignments:
        rect=[x*16-256,z*16-256,(x+1)*16-256,(z+1)*16-256]
        out,_=surface_format.paint(out,tileset,rect,height,shapes[material],storage_base=raw)
    require(len(out)<=0xF000,
            "Authored model exceeds the verified 60 KiB map buffer; use a smaller surface selection", "RESOURCE_CAPACITY")
    return out


def model(project,context,state):
    raw=map_data(project.member_raw(context['map_member']))[2]
    cells=state.get('surfaces',{}).get(context['map_member'],{})
    if not cells:return raw
    _,blobs=mapscene.tilesets(project,context)
    entries=tuple((x,z,v['material'],v['height'],tuple(v['affine']) if 'affine' in v else None)
                  if state.get('surface_versions',{}).get(context['map_member'],1)>=3 else
                  (x,z,v['material'],v['height']) for (x,z),v in sorted(cells.items()))
    return build(raw,blobs['map_tileset'],entries,state.get('surface_versions',{}).get(context['map_member'],1))


def plan(project,context,state,index,x,z,width,height,material=None,label=None,sample=None,_version=None):
    if _version is None:
        _version = 3 if sample is not None or state.get('surface_versions',{}).get(context['map_member'],1)>=3 else 2
    require(all(type(v) is int for v in (x,z,width,height)) and 1<=width<=8 and 1<=height<=8,
            'Choose an integer rectangle up to 8 by 8 tiles','INVALID_INPUT')
    require(label is None or isinstance(label,str) and 0<len(label)<=160,'Invalid surface label')
    from . import world
    world.cell_offset(context,x,z);world.cell_offset(context,x+width-1,z+height-1)
    choices=palette(project,context)
    choice=next((c for c in choices if c['material']==material),None)
    require(material is None or choice is not None,'Choose a compatible stock surface','UNSUPPORTED_SURFACE')
    current=copy.deepcopy(state.get('surfaces',{}).get(context['map_member'],{}));before=copy.deepcopy(current)
    ox,oz=context['origin']
    mapping = None
    if sample is not None:
        from .surface_native import sample_mapping
        require(_version == 3 and isinstance(sample,dict) and set(sample)=={'x','z'}, 'Sample needs x/z')
        world.cell_offset(context,sample['x'],sample['z'])
        _,blobs=mapscene.tilesets(project,context)
        mapping=sample_mapping(model(project,context,state),blobs['map_tileset'],sample['x']-ox,sample['z']-oz)
        require(mapping['material']==material,'Sample and chosen surface differ','UNSUPPORTED_SURFACE')
    for xx in range(x,x+width):
        for zz in range(z,z+height):
            key=(xx-ox,zz-oz)
            if material is None:current.pop(key,None)
            else:
                ground=scenery.floor_height(project,context,{'x':xx+0.5,'z':zz+0.5})*16
                # BDHC and visual surfaces have deliberate offsets. Select the
                # unique donor plane within two model units of the walking floor.
                distance=min(abs(y-ground) for y in choice['heights'])
                candidates=[y for y in choice['heights'] if abs(y-ground)==distance and distance<=2]
                require(len(candidates)==1,'Surface needs one matching flat donor height','UNSUPPORTED_HEIGHT')
                current[key]={'material':material,'height':candidates[0]}
                if mapping is not None:
                    require(abs(mapping['height']-candidates[0])<1e-4,'Sample must use the same floor height','UNSUPPORTED_HEIGHT')
                    current[key]['affine']=mapping['affine']
    require(len(current)<=64,'At most 64 authored surface tiles per resource','RESOURCE_CAPACITY')
    trial={**state,'surfaces':{**state.get('surfaces',{}),context['map_member']:current},
           'surface_versions':{**state.get('surface_versions',{}),context['map_member']:_version}}
    old=model(project,context,state);new=model(project,context,trial)
    changes=[{'x':k[0],'z':k[1],'before':before.get(k),'after':current.get(k)} for k in sorted(before.keys()|current.keys()) if before.get(k)!=current.get(k)]
    # Painting the original material is a visual no-op and does not write state.
    if old==new:changes=[]
    deps=authoring.dependencies(context,index)
    result={'schema':SCHEMAS[_version-1],'index':index,'context':authoring.context_ref(context),'label':label or 'Paint flat surface',
            'request':dict(x=x,z=z,width=width,height=height,material=material,label=label),'cells':changes,
            'before_sha256':digest(old),'after_sha256':digest(new),'dependencies':deps,'dependencies_sha256':authoring.canonical(deps)}
    if _version>=2:result['writer_before']=state.get('surface_versions',{}).get(context['map_member'],1)
    if _version==3:result['request']['sample']=sample
    return result


def replay(project,state,t,index):
    try:
        ctx=project.context(header=t['context']['header'],cell=t['context']['cell'])
        version=SCHEMAS.index(t['schema'])+1
        expected=plan(project,ctx,state,index,**t['request'],_version=version)
        require(t==expected and changed(t),'Surface before-value or dependency differs','BEFORE_VALUE_MISMATCH')
        cells=state.setdefault('surfaces',{}).setdefault(ctx['map_member'],{})
        for c in t['cells']:
            key=(c['x'],c['z'])
            if c['after'] is None:cells.pop(key,None)
            else:cells[key]=c['after']
        state.setdefault('surface_versions',{})[ctx['map_member']]=version
        state['contexts'].append(ctx)
    except (KeyError,TypeError,ValueError) as exc:raise EditorError('INVALID_INPUT','Malformed surface transaction') from exc


def summary(t):return {'operation':'surface.transaction','index':t['index'],'label':t['label'],'context':t['context'],'cells':t['cells']}


def changed(t):return bool(t['cells']) or (t['schema']!=SCHEMA and t['before_sha256']!=t['after_sha256'])
