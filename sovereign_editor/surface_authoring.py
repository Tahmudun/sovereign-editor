"""Deterministic surface requests rebuilt from the immutable model, never a prior export."""
import copy
import functools
import numpy as np
from . import authoring, mapscene, nitro, surface_format, scenery
from .formats import require,digest,map_data,EditorError

SCHEMA='sovereign-surface-transaction-v1'
NATIVE_SCHEMA='sovereign-surface-transaction-v2'
SAMPLED_SCHEMA='sovereign-surface-transaction-v3'
LARGE_SCHEMA='sovereign-surface-transaction-v4'
# v5: bordered path/tall-grass patches (border_authoring.py); v1-v4 replay unchanged.
BORDER_SCHEMA='sovereign-surface-transaction-v5'
SCHEMAS=(SCHEMA,NATIVE_SCHEMA,SAMPLED_SCHEMA,LARGE_SCHEMA,BORDER_SCHEMA)
# (rectangle side, authored tiles per map resource). v1-v3 keep their historical
# limits. v4 (terrain) accepts listed tiles; its geometry guard is the p90 of the
# pinned baseline's 675 map models (evidence/world-authoring-v1/terrain-budget.json).
LIMITS={1:(8,64),2:(8,64),3:(8,64),4:(16,256),5:(16,256)}
BUDGET={'polygons':1228,'vertices':3916}
# Stock tall grass is an 'egrass' plane two model units above flat ground (16/18).
OVERLAY_TEXTURES=('egrass',)


def palette(project,context):
    raw=map_data(project.member_raw(context['map_member']))[2]
    refs,blobs=mapscene.tilesets(project,context)
    summary,prims=nitro.decode_model(raw,tileset=blobs['map_tileset'],render=False)
    _,_,_,_,draws=surface_format.layout(raw)
    entries=[]
    for (sid,_,_),p in zip(draws,prims):
        name=p.material['texture_name'] or ''
        overlay=name in OVERLAY_TEXTURES
        if not (name in ('grass01gs','grass02','road01') or 'yuka' in name or overlay):continue
        if not all(p.material['repeat']):continue
        heights=sorted({float(p.vertices[t[0],1]) for t in p.triangles if np.ptp(p.vertices[t,1])<1e-4})
        if heights:entries.append({'material':p.material['name'],'texture':name,'shape':sid,'heights':heights,
                                   **({'overlay':True} if overlay else {})})
    return entries


def counts(raw):
    """(vertices, polygons) recorded in a map model's MDL0 header."""
    import struct
    _,_,mdl,_,_=surface_format.layout(raw)
    vertices,polygons,_,_=struct.unpack_from('<4H',mdl,36)
    return vertices,polygons


@functools.lru_cache(maxsize=32)
def build(raw,tileset,assignments,version=1,custom=()):
    if not assignments:return raw
    if version>=2:
        from . import surface_native
        return surface_native.build(raw,tileset,assignments,custom) if custom else surface_native.build(raw,tileset,assignments)
    _,prims=nitro.decode_model(raw,tileset=tileset,render=False)
    _,_,_,_,draws=surface_format.layout(raw)
    shapes={p.material['name']:sid for (sid,_,_),p in zip(draws,prims)}
    out=raw
    for x,z,material,height in assignments:
        rect=[x*16-256,z*16-256,(x+1)*16-256,(z+1)*16-256]
        out,_=surface_format.paint(out,tileset,rect,height,shapes[material],storage_base=raw)
    require(len(out)<=0xF000,
            "Authored model exceeds the verified 60 KiB map buffer; use a smaller surface selection", "RESOURCE_CAPACITY")
    return out


_MODELS={}          # memo: every input of one cell's model -> its bytes (replay re-plans each edit)
_MODELS_LIMIT=64


def _model_key(project,context,state):
    import json
    member=context['map_member']
    part=lambda key:(state.get(key) or {}).get(member)
    cells=part('surfaces') or {}
    inputs={'surfaces':sorted([list(k),v] for k,v in cells.items()),'pieces':part('surface_pieces'),
            'version':part('surface_versions'),'features':part('terrain_features'),'decals':part('ground_decals'),
            'materials':state.get('ground_materials')}
    return (project.doc['baseline']['sha256'],member,digest(project.member_raw(member)),
            context['area_data']['id'],tuple(context['origin']),json.dumps(inputs,sort_keys=True,default=str))


def model(project,context,state):
    """Stock paint, then terrain features, then project ground-material paint (so it can pave
    terrace tops; stock-only histories compose exactly as before), then draped decals."""
    key=_model_key(project,context,state)
    if key in _MODELS:
        return _MODELS[key]
    result=_model(project,context,state)
    if len(_MODELS)>=_MODELS_LIMIT:
        _MODELS.pop(next(iter(_MODELS)))
    _MODELS[key]=result
    return result


def _model(project,context,state):
    from . import ground_materials
    member=context['map_member']
    cells=state.get('surfaces',{}).get(member,{})
    custom_cells={k:v for k,v in cells.items() if ground_materials.is_custom(v.get('material'))}
    if custom_cells:
        stock_state={**state,'surfaces':{**state['surfaces'],member:{k:v for k,v in cells.items() if k not in custom_cells}}}
        result=_surfaced(project,context,stock_state)
        _,blobs=mapscene.tilesets(project,context,state)
        entries=tuple((x,z,v['material'],v['height'],tuple(v['affine']),'replace')
                      for (x,z),v in sorted(custom_cells.items()))
        result=build(result,blobs['map_tileset'],entries,5,ground_materials.custom_for(project,state,context))
    else:
        result=_surfaced(project,context,state)
    if (state.get('ground_decals') or {}).get(context['map_member']):
        from . import decals
        _,blobs=mapscene.tilesets(project,context,state)
        result=decals.drape(project,state,context,result,blobs['map_tileset'])
    return result


def _surfaced(project,context,state):
    raw=map_data(project.member_raw(context['map_member']))[2]
    cells=state.get('surfaces',{}).get(context['map_member'],{})
    overlay=state.get('surface_pieces',{}).get(context['map_member'],{})
    if not cells and not overlay:return _terrain(project,context,state,raw)
    _,blobs=mapscene.tilesets(project,context,state)
    version=state.get('surface_versions',{}).get(context['map_member'],1)
    entries=tuple((x,z,v['material'],v['height'],tuple(v['affine']) if 'affine' in v else None,v.get('mode'))
                  if version>=5 else
                  (x,z,v['material'],v['height'],tuple(v['affine']) if 'affine' in v else None)
                  if version>=3 else
                  (x,z,v['material'],v['height']) for (x,z),v in sorted(cells.items()))
    custom=()
    if state.get('ground_materials'):
        from . import ground_materials
        custom=ground_materials.custom_for(project,state,context)
    result=build(raw,blobs['map_tileset'],entries,version,custom) if cells else raw
    if overlay:
        pieces=tuple((v['material'],*v['rect'],v['height'],*v['uv']) for _,v in sorted(overlay.items()))
        result=_pieces(result,blobs['map_tileset'],pieces)
    return _terrain(project,context,state,result)


def _terrain(project,context,state,result):
    if not state.get('terrain_features',{}).get(context['map_member']):return result
    from . import terrain_authoring
    return terrain_authoring.compose_model(project,context,state,result)


@functools.lru_cache(maxsize=32)
def _pieces(raw,tileset,pieces):
    from . import surface_native
    return surface_native.add_pieces(raw,tileset,pieces)


def plan(project,context,state,index,x=None,z=None,width=None,height=None,material=None,label=None,sample=None,tiles=None,_version=None):
    writer=state.get('surface_versions',{}).get(context['map_member'],1)
    if _version is None:
        _version = 4 if tiles is not None or writer>=4 else 3 if sample is not None or writer>=3 else 2
    side,total=LIMITS[_version]
    from . import world
    if tiles is not None:
        require(_version>=4 and all(v is None for v in (x,z,width,height)),'Listed tiles use the v4 terrain writer','INVALID_INPUT')
        require(isinstance(tiles,list) and 1<=len(tiles)<=total and all(isinstance(t,dict) and set(t)=={'x','z'}
                and type(t['x']) is int and type(t['z']) is int for t in tiles),f'Choose 1..{total} tiles with integer x/z','INVALID_INPUT')
        coords=[(t['x'],t['z']) for t in tiles]
        require(len(set(coords))==len(coords),'Tiles must be distinct','INVALID_INPUT')
    else:
        require(all(type(v) is int for v in (x,z,width,height)) and 1<=width<=side and 1<=height<=side,
                f'Choose an integer rectangle up to {side} by {side} tiles','INVALID_INPUT')
        coords=[(xx,zz) for xx in range(x,x+width) for zz in range(z,z+height)]
    require(label is None or isinstance(label,str) and 0<len(label)<=160,'Invalid surface label')
    for xx,zz in coords:world.cell_offset(context,xx,zz)
    if state.get('terrain_features'):
        from . import terrain_authoring
        terrain_authoring.require_free(state,context['header']['id'],coords,'Surface paint')
    choices=palette(project,context)
    choice=next((c for c in choices if c['material']==material),None)
    require(material is None or choice is not None,'Choose a compatible stock surface','UNSUPPORTED_SURFACE')
    require(choice is None or not choice.get('overlay') or _version>=4,'Tall grass needs the v4 terrain writer','UNSUPPORTED_SURFACE')
    current=copy.deepcopy(state.get('surfaces',{}).get(context['map_member'],{}));before=copy.deepcopy(current)
    ox,oz=context['origin']
    mapping = None
    if sample is not None:
        from .surface_native import sample_mapping
        require(_version == 3 and isinstance(sample,dict) and set(sample)=={'x','z'}, 'Sample needs x/z')
        world.cell_offset(context,sample['x'],sample['z'])
        _,blobs=mapscene.tilesets(project,context,state)
        mapping=sample_mapping(model(project,context,state),blobs['map_tileset'],sample['x']-ox,sample['z']-oz)
        require(mapping['material']==material,'Sample and chosen surface differ','UNSUPPORTED_SURFACE')
    for xx,zz in coords:
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
    require(len(current)<=total,f'At most {total} authored surface tiles per resource','RESOURCE_CAPACITY')
    trial={**state,'surfaces':{**state.get('surfaces',{}),context['map_member']:current},
           'surface_versions':{**state.get('surface_versions',{}),context['map_member']:_version}}
    old=model(project,context,state);new=model(project,context,trial)
    if _version>=4 and new!=old:
        (v0,p0),(v1,p1)=counts(old),counts(new)
        require(p1<=max(BUDGET['polygons'],p0) and v1<=max(BUDGET['vertices'],v0),
                f'Edited model needs {p1} polygons/{v1} vertices; the measured budget is '
                f"{BUDGET['polygons']}/{BUDGET['vertices']}. Choose fewer tiles",'RESOURCE_CAPACITY')
    changes=[{'x':k[0],'z':k[1],'before':before.get(k),'after':current.get(k)} for k in sorted(before.keys()|current.keys()) if before.get(k)!=current.get(k)]
    # Painting the original material is a visual no-op and does not write state.
    if old==new:changes=[]
    deps=authoring.dependencies(context,index)
    result={'schema':SCHEMAS[_version-1],'index':index,'context':authoring.context_ref(context),'label':label or 'Paint flat surface',
            'request':dict(x=x,z=z,width=width,height=height,material=material,label=label),'cells':changes,
            'before_sha256':digest(old),'after_sha256':digest(new),'dependencies':deps,'dependencies_sha256':authoring.canonical(deps)}
    if _version>=2:result['writer_before']=state.get('surface_versions',{}).get(context['map_member'],1)
    if _version>=3:result['request']['sample']=sample
    if _version>=4:result['request']['tiles']=tiles
    return result


def replay(project,state,t,index):
    if t.get('schema')==BORDER_SCHEMA:
        from . import border_authoring
        return border_authoring.replay(project,state,t,index)
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


def summary(t):
    result={'operation':'surface.transaction','index':t['index'],'label':t['label'],'context':t['context'],'cells':t['cells']}
    if t['schema']==BORDER_SCHEMA:result.update(family=t['request']['family'],pieces=len(t['pieces']))
    return result


def changed(t):
    if t['schema']==BORDER_SCHEMA:
        from . import border_authoring
        return border_authoring.changed(t)
    return bool(t['cells']) or (t['schema']!=SCHEMA and t['before_sha256']!=t['after_sha256'])
