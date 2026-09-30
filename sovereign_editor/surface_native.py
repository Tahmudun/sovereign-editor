"""Flat surface writer that retains native polygon topology and shading semantics.

v1 remains available solely for exact historical transaction/Undo replay. This
writer clips only polygons touched by paint; unaffected quads stay native quads.
COLOR and NORMAL are alternative sources of vertex color on the DS, not two
independent attributes. A synthetic NORMAL must never follow a baked COLOR.
"""
import struct
import numpy as np
from . import nitro, surface_format as legacy
from .formats import require


def sample_mapping(raw, tileset, x, z, prims=None):
    """Native planar UV mapping at a local tile center, with no invented scaling."""
    return mapping_sampler(raw, tileset, prims)(x, z)


def mapping_sampler(raw, tileset, prims=None):
    """Sampler for many tiles of one model: the triangle systems are stacked once and
    solved together per tile (the same per-triangle LAPACK solves as one at a time)."""
    if prims is None:
        _, prims = nitro.decode_model(raw, tileset=tileset, render=False)
    owners, systems, heights = [], [], []
    for p in prims:
        for tri in p.triangles:
            v = p.vertices[tri]
            a = np.column_stack([v[:, 0], v[:, 2], np.ones(3)])
            owners.append((p, tri, a))
            systems.append(a.T)
            heights.append(v[:, 1])
    systems = np.array(systems, dtype=float).reshape(-1, 3, 3)
    heights = np.array(heights, dtype=float).reshape(-1, 3)
    usable = np.abs(np.linalg.det(systems)) >= 1e-6 if len(systems) else np.zeros(0, bool)
    index = np.nonzero(usable)[0]
    systems, heights = systems[index], heights[index]

    def sample(x, z):
        point = np.array([((x + .5) * 16 - 256), ((z + .5) * 16 - 256), 1.0])
        weights = np.linalg.solve(systems, np.broadcast_to(point, (len(systems), 3))[..., None])[..., 0] \
            if len(systems) else np.zeros((0, 3))
        inside = np.nonzero(weights.min(axis=1) >= -1e-6)[0]
        candidates = [(float(weights[i] @ heights[i]), float(min(weights[i])), *owners[index[i]]) for i in inside]
        require(candidates, 'No floor at this tile', 'UNSUPPORTED_SURFACE')
        height = max(c[0] for c in candidates)
        top = [c for c in candidates if abs(c[0] - height) < .01]
        require(len({c[2].material['name'] for c in top}) == 1, 'Ambiguous overlapping surfaces', 'UNSUPPORTED_SURFACE')
        _, _, p, tri, a = max(top, key=lambda c: c[1])
        require(np.ptp(p.vertices[tri, 1]) < 1e-4 and all(p.material['repeat']),
                'Sample a flat repeating floor', 'UNSUPPORTED_SURFACE')
        return {'material': p.material['name'], 'height': height,
                'affine': np.linalg.solve(a, p.uvs[tri]).round(12).reshape(-1).tolist()}
    return sample


def fragments(poly):
    # Keep a clipped quad as a quad. Remove consecutive duplicate boundary points.
    clean=[]
    for v in poly:
        if not clean or np.linalg.norm(v[:3]-clean[-1][:3])>1e-7:clean.append(v)
    if len(clean)>1 and np.linalg.norm(clean[0][:3]-clean[-1][:3])<1e-7:clean.pop()
    if len(clean)<3 or area(clean)<1e-5:return []
    return [np.array(clean)] if len(clean)<=4 else legacy.tris(clean)


def area(poly):
    return sum(abs(np.cross(poly[i][:3]-poly[0][:3],poly[i+1][:3]-poly[0][:3])[1])/2
               for i in range(1,len(poly)-1))


def encode(polygons, scale, width, height):
    require(polygons,'Cannot empty a material shape','UNSUPPORTED_SURFACE')
    for poly in polygons:require(len(poly) in (3,4),'Invalid native polygon')
    # Per-vertex values are computed for all vertices at once (elementwise, so
    # identical to one vertex at a time); the first failing vertex reports the
    # same check it did before.
    V=np.concatenate(polygons)
    coords=np.rint(V[:,:3]/scale*4096).astype(int)
    uvs=np.rint(V[:,3:5]*[width,height]*16).astype(int)
    kinds=V[:,9].astype(int)
    cols=np.clip(np.rint(V[:,5:8]*31),0,31).astype(int)
    bad=[np.any((coords<-32768)|(coords>32767),axis=1),np.any((uvs<-32768)|(uvs>32767),axis=1),
         (kinds!=0x20)&(kinds!=0x21)]
    failing=np.nonzero(bad[0]|bad[1]|bad[2])[0]
    if len(failing):
        i=failing[0]
        require(not bad[0][i],'Vertex outside VTX_16 range')
        require(not bad[1][i],'UV outside TEXCOORD range')
        require(False,'Inherited lighting state is unsupported','UNSUPPORTED_SURFACE')
    values=np.where(kinds==0x20,cols[:,0]|cols[:,1]<<5|cols[:,2]<<10,V[:,8].astype(np.int64)).tolist()
    packed=np.all(coords%64==0,axis=1)&np.all(coords//64>=-512,axis=1)&np.all(coords//64<=511,axis=1)
    small=(((coords//64)&1023)<<np.array([0,10,20])).sum(axis=1).tolist()
    coords_l=coords.tolist();uvs_l=uvs.tolist();kinds_l=kinds.tolist();packed_l=packed.tolist()
    commands=[];mode=None;last_shade=last_uv=None;k=0
    for poly in polygons:
        wanted=0 if len(poly)==3 else 1
        if wanted!=mode:
            if mode is not None:commands.append((0x41,b''))
            commands.append((0x40,struct.pack('<I',wanted)));mode=wanted
        for _ in range(len(poly)):
            shade=(kinds_l[k],int(values[k]))
            if shade!=last_shade:commands.append((shade[0],struct.pack('<I',shade[1])));last_shade=shade
            uv=tuple(uvs_l[k])
            if uv!=last_uv:commands.append((0x22,struct.pack('<2h',*uv)));last_uv=uv
            if packed_l[k]:commands.append((0x24,struct.pack('<I',int(small[k]))))
            else:commands.append((0x23,struct.pack('<3hH',*coords_l[k],0)))
            k+=1
    commands.append((0x41,b''));result=bytearray()
    for i in range(0,len(commands),4):
        packet=commands[i:i+4]
        result.extend(bytes(c for c,_ in packet)+bytes(4-len(packet)))
        for _,params in packet:result.extend(params)
    return bytes(result)


def _box(poly):
    """XZ bounds of a polygon, for skipping clips that cannot overlap a rectangle."""
    xs,zs=poly[:,0],poly[:,2]
    return (float(xs.min()),float(xs.max()),float(zs.min()),float(zs.max()))


def _apart(box,rect):
    """True when the polygon cannot cover positive area inside rect (exact skip:
    clipping would leave a degenerate piece whose area is below every threshold)."""
    return box[1]<=rect[0] or box[0]>=rect[2] or box[3]<=rect[1] or box[2]>=rect[3]


GROUND=('grass01gs','grass02','road01','road01_r','road01_sub','grass02_r')
OVERLAY_TEXTURES=('egrass',)
OVERLAY_RISE=2   # stock tall grass: egrass plane 18 over flat ground 16 (Route 29/30)


def overlay(polys,by_shape,donor_id,donor,dtri,affine,rect,height,boxes=None):
    """Add one stock-style tall-grass quad above complete flat ground; no clipping.

    Returns False when the tile already shows this grass. Border trim pieces
    are not synthesized; the patch edge is square.
    """
    ground=existing=0
    for sid,p in by_shape.items():
        for k,poly in enumerate(polys[sid]):
            if boxes is not None and _apart(boxes[sid][k],rect):continue
            _,inside=legacy.pieces(list(poly),rect)
            projected=area(inside)
            if projected<1e-5:continue
            top=np.max(poly[:,1])
            require(top<=height+1e-4,'This tile is covered by raised terrain, edging or baked scenery','UNSUPPORTED_SURFACE')
            if np.max(np.abs(poly[:,1]-height))<=1e-4:
                require(sid==donor_id,'Tall grass would overlap another raised surface','UNSUPPORTED_SURFACE')
                existing+=projected;continue
            name=p.material['texture_name'] or ''
            require(np.ptp(poly[:,1])<1e-4 and abs(top-(height-OVERLAY_RISE))<=1e-4 and (name in GROUND or 'yuka' in name),
                    'Tall grass needs complete flat ordinary ground two units below','UNSUPPORTED_SURFACE')
            ground+=projected
    if abs(existing-256)<0.01:return False
    require(existing<1e-5 and abs(ground-256)<0.01,
            'Choose tiles with one complete flat ground surface for tall grass','UNSUPPORTED_SURFACE')
    x0,z0,x1,z1=rect
    corners=[(x0,z0),(x0,z1),(x1,z1),(x1,z0)]
    reference=donor.vertices[donor.polygons[0]][:,[0,2]]
    signed=lambda pts:sum(a[0]*b[1]-b[0]*a[1] for a,b in zip(pts,list(pts[1:])+[pts[0]]))
    if (signed(corners)>0)!=(signed([tuple(v) for v in reference])>0):corners.reverse()
    quad=np.zeros((4,10))
    quad[:,0]=[c[0] for c in corners];quad[:,1]=height;quad[:,2]=[c[1] for c in corners]
    quad[:,3:5]=np.column_stack([quad[:,0],quad[:,2],np.ones(4)])@affine
    quad[:,5:8]=donor.colors[dtri[0]];quad[:,8]=donor.normals[dtri[0]];quad[:,9]=donor.shade_commands[dtri[0]]
    polys[donor_id].append(quad)
    if boxes is not None:boxes[donor_id].append(_box(quad))
    return True


def build(raw,tileset,assignments,custom=()):
    """``custom``: ((texture, width, height), ...) project ground-material textures the area
    tileset provides; a tile painted with one of them joins a new shape of that material
    (appended to the model), shaded like the ground face it replaces."""
    if not assignments:return raw
    summary,prims=nitro.decode_model(raw,tileset=tileset,render=False)
    bo,mo,model,shape,draws=legacy.layout(raw)
    require(all(np.allclose(m,np.eye(4)) for _,_,m in draws),'Surface writing requires identity node transforms')
    by_shape={sid:p for (sid,_,_),p in zip(draws,prims)}
    shapes={p.material['name']:sid for sid,p in by_shape.items()}
    extra={name:(w,h) for name,w,h in custom}
    new_polys={}
    polys={}
    for sid,p in by_shape.items():
        arr=np.column_stack([p.vertices,p.uvs,p.colors,p.normals,p.shade_commands])
        polys[sid]=[arr[indices].copy() for indices in p.polygons]
    boxes={sid:[_box(poly) for poly in polys[sid]] for sid in polys}
    changed=set()
    for assignment in assignments:
        x,z,material,height = assignment[:4]
        sampled = assignment[4] if len(assignment) > 4 else None
        # v5 'replace': re-map even same-material faces (a rim turned toward a new
        # path) and keep transparent stock grass overlays above the repainted ground.
        replace = len(assignment) > 5 and assignment[5] == 'replace'
        rect=[x*16-256,z*16-256,(x+1)*16-256,(z+1)*16-256]
        if material not in shapes:
            require(material in extra and sampled is not None,f'This map model has no {material} surface',
                    'UNSUPPORTED_SURFACE')
            _paint_custom(polys,boxes,by_shape,new_polys.setdefault(material,[]),rect,height,
                          np.asarray(sampled,dtype=float).reshape(3,2),changed)
            continue
        donor_id=shapes[material];donor=by_shape[donor_id]
        require(donor.material['texture_name'] and all(donor.material['repeat']),'Donor must repeat both axes')
        dtri=next((t for t in donor.triangles if np.max(np.abs(donor.vertices[t,1]-height))<1e-4),None)
        require(dtri is not None,'Donor needs a flat face at target height')
        v=donor.vertices[dtri]
        affine=np.linalg.solve(np.column_stack([v[:,0],v[:,2],np.ones(3)]),donor.uvs[dtri])
        if sampled is not None:
            affine = np.asarray(sampled, dtype=float).reshape(3, 2)
        if (donor.material['texture_name'] or '') in OVERLAY_TEXTURES:
            if overlay(polys,by_shape,donor_id,donor,dtri,affine,rect,height,boxes):changed.add(donor_id)
            continue
        covered=0;added=[]
        for sid,p in by_shape.items():
            output=[];kept=[]
            for poly,box in zip(polys[sid],boxes[sid]):
                if _apart(box,rect):output.append(poly);kept.append(box);continue
                outside,inside=legacy.pieces(list(poly),rect)
                projected=area(inside)
                if projected<1e-5:output.append(poly);kept.append(box);continue
                if replace and (p.material['texture_name'] or '') in PIECE_TEXTURES and np.min(poly[:,1])>=height+OVERLAY_RISE-1e-4:
                    output.append(poly);kept.append(box);continue
                if (replace and (p.material['texture_name'] or '') in DECAL_TEXTURES and np.ptp(poly[:,1])<1e-4
                        and abs(poly[0,1]-height-1)<=1e-4):
                    # A cosmetic stock decal one unit above repainted ground is removed
                    # with it (ledges and other raised geometry still refuse).
                    parts=[part for piece in outside for part in fragments(piece)]
                    output.extend(parts);kept.extend(_box(part) for part in parts)
                    changed.add(sid);continue
                require(np.max(poly[:,1])<=height+1e-4,
                        'This surface is covered by raised terrain, edging or baked scenery','UNSUPPORTED_SURFACE')
                if np.max(np.abs(poly[:,1]-height))>1e-4:output.append(poly);kept.append(box);continue
                name=p.material['texture_name'] or ''
                require(name in ('grass01gs','grass02','road01','road01_r','road01_sub','grass02_r') or 'yuka' in name,
                        'This face is not an ordinary ground or floor surface','UNSUPPORTED_SURFACE')
                covered+=projected
                # Existing source faces keep their native mapping when painting
                # the same material. A sampled donor maps newly filled faces.
                if sid==donor_id and not replace:output.append(poly);kept.append(box);continue
                require(len(set(poly[:,9]))==1 and (poly[0,9]!=0x21 or len(set(poly[:,8]))==1),
                        'Interpolated lighting normals are unsupported','UNSUPPORTED_SURFACE')
                changed.add(sid)
                parts=[part for piece in outside for part in fragments(piece)]
                output.extend(parts);kept.extend(_box(part) for part in parts)
                for part in fragments(inside):
                    part[:,3:5]=np.column_stack([part[:,0],part[:,2],np.ones(len(part))])@affine
                    part[:,5:8]=donor.colors[dtri[0]];part[:,8]=donor.normals[dtri[0]]
                    part[:,9]=donor.shade_commands[dtri[0]];added.append(part)
            polys[sid]=output;boxes[sid]=kept
        require(abs(covered-256)<0.01,
                'Choose tiles with one complete flat surface; this patch includes a gap, overlap or height change','UNSUPPORTED_SURFACE')
        if added:
            polys[donor_id].extend(added);boxes[donor_id].extend(_box(part) for part in added);changed.add(donor_id)
    for sid in sorted(changed):
        # A shape cannot be encoded empty, so when repainting would remove the last stock decal
        # of its material in this map, that cosmetic decal stays (one unit above the new ground).
        if not polys[sid] and (by_shape[sid].material['texture_name'] or '') in DECAL_TEXTURES:
            p=by_shape[sid];arr=np.column_stack([p.vertices,p.uvs,p.colors,p.normals,p.shade_commands])
            polys[sid]=[arr[indices].copy() for indices in p.polygons];changed.discard(sid)
    if not changed and not any(new_polys.values()):return raw
    new={name:(extra[name],pp) for name,pp in new_polys.items() if pp}
    return _rewrite(raw,tileset,summary,bo,mo,model,shape,draws,by_shape,polys,changed,new)


def _paint_custom(polys,boxes,by_shape,out,rect,height,affine,changed):
    """Replace one complete flat ordinary ground tile by ``out`` (a custom material's polygons),
    copying the replaced face's lighting (COLOR or NORMAL) so the tile shades like its ground."""
    covered=0;shade=None
    for sid,p in by_shape.items():
        output=[];kept=[]
        for poly,box in zip(polys[sid],boxes[sid]):
            if _apart(box,rect):output.append(poly);kept.append(box);continue
            outside,inside=legacy.pieces(list(poly),rect)
            projected=area(inside)
            if projected<1e-5:output.append(poly);kept.append(box);continue
            name=p.material['texture_name'] or ''
            if name in PIECE_TEXTURES and np.min(poly[:,1])>=height+OVERLAY_RISE-1e-4:
                output.append(poly);kept.append(box);continue
            if name in DECAL_TEXTURES and np.ptp(poly[:,1])<1e-4 and abs(poly[0,1]-height-1)<=1e-4:
                parts=[part for piece in outside for part in fragments(piece)]
                output.extend(parts);kept.extend(_box(part) for part in parts);changed.add(sid);continue
            require(np.max(poly[:,1])<=height+1e-4,
                    'This surface is covered by raised terrain, edging or baked scenery','UNSUPPORTED_SURFACE')
            if np.max(np.abs(poly[:,1]-height))>1e-4:output.append(poly);kept.append(box);continue
            require(name in GROUND or 'yuka' in name or name.startswith(CUSTOM_PREFIX),
                    f'Tile {(rect[0]+256)//16:g},{(rect[1]+256)//16:g} (cell-local) shows {name or "an untextured face"}, '
                    'not ordinary ground','UNSUPPORTED_SURFACE')
            require(len(set(poly[:,9]))==1 and (poly[0,9]!=0x21 or len(set(poly[:,8]))==1),
                    'Interpolated lighting normals are unsupported','UNSUPPORTED_SURFACE')
            covered+=projected;changed.add(sid)
            if shade is None:shade=(poly[0,5:8].copy(),poly[0,8],poly[0,9])
            parts=[part for piece in outside for part in fragments(piece)]
            output.extend(parts);kept.extend(_box(part) for part in parts)
        polys[sid]=output;boxes[sid]=kept
    # A tile already painted with a custom material sits in ``out`` itself.
    remaining=[]
    for poly in out:
        if _apart(_box(poly),rect):remaining.append(poly);continue
        outside,inside=legacy.pieces(list(poly),rect)
        projected=area(inside)
        if projected<1e-5:remaining.append(poly);continue
        covered+=projected
        if shade is None:shade=(poly[0,5:8].copy(),poly[0,8],poly[0,9])
        remaining.extend(part for piece in outside for part in fragments(piece))
    out[:]=remaining
    require(abs(covered-256)<0.01,
            'Choose tiles with one complete flat surface; this patch includes a gap, overlap or height change','UNSUPPORTED_SURFACE')
    x0,z0,x1,z1=rect
    quad=np.zeros((4,10))
    quad[:,0]=[x0,x1,x1,x0];quad[:,1]=height;quad[:,2]=[z0,z0,z1,z1]
    if _signed_xz(quad)>0:quad=quad[::-1].copy()
    quad[:,3:5]=np.column_stack([quad[:,0],quad[:,2],np.ones(4)])@affine
    quad[:,5:8]=shade[0];quad[:,8]=shade[1];quad[:,9]=shade[2]
    out.append(quad)


def _signed_xz(poly):
    pts=poly[:,[0,2]]
    return sum(a[0]*b[1]-b[0]*a[1] for a,b in zip(pts,np.roll(pts,-1,axis=0)))


CUSTOM_PREFIX='g_'


def _template_record(model,mat_section,by_shape):
    """Material record of the model's first stock ground material (lighting/diffuse template)."""
    from . import nitro_writer as nw
    entries,_=nw.read_dictionary(mat_section,4)
    records={}
    for name,value in entries:
        at=struct.unpack('<I',value)[0];size=struct.unpack_from('<H',mat_section,at+2)[0]
        records[name]=bytes(mat_section[at:at+size])
    for p in by_shape.values():
        if (p.material['texture_name'] or '') in GROUND:
            return records[p.material['name']]
    require(False,'This map model has no stock ground material to copy lighting from','UNSUPPORTED_SURFACE')


def _decode(raw,tileset):
    summary,prims=nitro.decode_model(raw,tileset=tileset,render=False)
    bo,mo,model,shape,draws=legacy.layout(raw)
    require(all(np.allclose(m,np.eye(4)) for _,_,m in draws),'Surface writing requires identity node transforms')
    by_shape={sid:p for (sid,_,_),p in zip(draws,prims)}
    polys={}
    for sid,p in by_shape.items():
        arr=np.column_stack([p.vertices,p.uvs,p.colors,p.normals,p.shade_commands])
        polys[sid]=[arr[indices].copy() for indices in p.polygons]
    return summary,bo,mo,model,shape,draws,by_shape,polys


def _rewrite(raw,tileset,summary,bo,mo,model,shape,draws,by_shape,polys,changed,new=None):
    """Re-encode the changed shapes and update the model/container headers. ``new``:
    {texture: ((width, height), polygons)} custom ground/decal materials appended as new
    materials and shapes (nitro_writer.extend_map_model)."""
    result=bytearray(model);entries=nitro.info(model,shape)
    old_t=old_q=new_t=new_q=0
    for sid,_,_ in draws:
        if sid not in changed:continue
        pos=shape+struct.unpack('<I',entries[sid][1])[0]
        dl,length=struct.unpack_from('<2I',model,pos+8)
        t,q=legacy.counts(model[pos+dl:pos+dl+length]);old_t+=t;old_q+=q
        new_t+=sum(len(poly)==3 for poly in polys[sid]);new_q+=sum(len(poly)==4 for poly in polys[sid])
        p=by_shape[sid]
        encoded=encode(polys[sid],summary['position_scale'],p.material['width'],p.material['height'])
        offset=len(result);result.extend(encoded);struct.pack_into('<II',result,pos+8,offset-pos,len(encoded))
    vertices=sum(sum(len(poly) for poly in polys[sid]) if sid in changed else len(p.vertices) for sid,p in by_shape.items())
    _,_,t,q=struct.unpack_from('<4H',model,36);t+=new_t-old_t;q+=new_q-old_q
    additions=[]
    if new:
        from . import nitro_writer as nw
        mat_off=struct.unpack_from('<I',model,8)[0]
        template=_template_record(model,model[mat_off:shape],by_shape)
        # Names of materials with shapes; an unused stock material of the same name (an animation
        # slot) keeps its name and is reused by the writer when it binds the same texture.
        taken={pp.material['name'] for pp in by_shape.values()}
        for name,value in sorted(new.items()):
            # (size, polygons[, palette]): Project ground materials use their own palette name;
            # a stock texture newly used in a cell names its stock palette (e.g. r_climb_pl).
            from .ground_materials import palette_name
            (w,h),pp=value[:2];palette=value[2] if len(value)>2 else palette_name(name)
            # A stock model may already use the texture's name for another material: the new
            # material then takes a distinct name (names only index dictionaries; bindings are by texture).
            material=name if name not in taken else next(f'{name[:13]}_{i}' for i in range(1,99) if f'{name[:13]}_{i}' not in taken)
            taken.add(material)
            addition={'name':material,'texture':name,'palette':palette,
                      'record':nw.map_material_record(template,w,h),
                      'display_list':encode(pp,summary['position_scale'],w,h)}
            if len(value)>3 and value[3]:
                # Area-animated water (waterfall v2): the writer draws it on the model's unbound
                # stock slot of that name, which the area's texture animation moves by index.
                addition['name']=name;addition['animate']=True
            additions.append(addition)
            vertices+=sum(len(poly) for poly in pp);t+=sum(len(poly)==3 for poly in pp);q+=sum(len(poly)==4 for poly in pp)
            polys[('new',name)]=pp
    require(max(vertices,t+q,t,q)<=65535,'Surface geometry exceeds Nitro metadata','RESOURCE_CAPACITY')
    struct.pack_into('<4H',result,36,vertices,t+q,t,q)
    # The model's native bounding box (6 x s16 at +44, quantum box_scale/4096) is rewritten only
    # when the geometry leaves it (carved caves); edits inside the stored box stay byte-identical.
    allv=np.concatenate([np.asarray(poly)[:,:3] for sid in polys for poly in polys[sid]])
    lo,hi=allv.min(0),allv.max(0)
    quantum=struct.unpack_from('<i',model,56)[0]/4096/4096
    box=np.array(struct.unpack_from('<6h',model,44))*quantum
    if np.max(np.abs(np.concatenate([lo-box[:3],hi-(box[:3]+box[3:])])))>2*quantum:
        scale=struct.unpack_from('<i',model,56)[0]
        while True:
            quantum=scale/4096/4096
            start=np.floor(lo/quantum);size=np.ceil(hi/quantum)-start
            values=[int(v) for v in list(start)+list(size)]
            if all(-32768<=v<=32767 for v in values):break
            # Coarser box quantum (stock maps use 128 with inverse 1/128); the inverse follows.
            require(scale<(1<<24),'Geometry extent exceeds the model bounding box range','RESOURCE_CAPACITY')
            scale*=2
        struct.pack_into('<6h',result,44,*values)
        struct.pack_into('<2i',result,56,scale,round(4096*4096/scale))
    struct.pack_into('<I',result,0,len(result));struct.pack_into('<I',result,16,len(result))
    if additions:
        from . import nitro_writer as nw
        result=bytearray(nw.extend_map_model(bytes(result),additions))
    output=bytearray(raw[:bo+mo])+result
    struct.pack_into('<I',output,bo+4,len(output)-bo);struct.pack_into('<I',output,8,len(output))
    require(len(output)<=0xf000,'Authored model exceeds the 60 KiB map buffer; use a smaller selection','RESOURCE_CAPACITY')
    nitro.decode_model(bytes(output),tileset=tileset,render=False)
    return bytes(output)


PIECE_TEXTURES=('egrass','egrass_u','egrass_v','egrass_ro','egrass_ri')
DECAL_TEXTURES=('road01_sub','grass01_a','grass02_a','flower01','flower02','flower03')


def add_pieces(raw,tileset,pieces):
    """Append stock border/overlay quads with explicit UVs (surface v5).

    Each piece is (texture, x0, z0, x1, z1, height, u0, v0, u1, v1, u2, v2, u3, v3)
    in model units, corners in x0z0,x1z0,x1z1,x0z1 order. The quad joins the map's
    own shape for that stock texture and must sit OVERLAY_RISE above complete flat
    ordinary ground, with nothing raised or already overlaid there.
    """
    if not pieces:return raw
    summary,bo,mo,model,shape,draws,by_shape,polys=_decode(raw,tileset)
    boxes={sid:[_box(poly) for poly in polys[sid]] for sid in polys}
    by_texture={}
    for sid,p in by_shape.items():by_texture.setdefault(p.material['texture_name'],[]).append(sid)
    changed=set()
    for piece in pieces:
        name,x0,z0,x1,z1,height=piece[:6]
        uv=np.array(piece[6:14],dtype=float).reshape(4,2)
        sids=by_texture.get(name) or []
        require(len(sids)==1,f'This map model has no single stock {name} shape; choose donor cells with the complete border family','UNSUPPORTED_BORDER')
        donor_id=sids[0];donor=by_shape[donor_id]
        rect=[x0,z0,x1,z1];ground=0.0
        for sid,p in by_shape.items():
            texture=p.material['texture_name'] or ''
            for poly,box in zip(polys[sid],boxes[sid]):
                if _apart(box,rect):continue
                _,inside=legacy.pieces(list(poly),rect)
                projected=area(inside)
                if projected<1e-5:continue
                top=np.max(poly[:,1])
                require(not(texture in PIECE_TEXTURES and np.max(np.abs(poly[:,1]-height))<=1e-4),
                        f'Tall grass or a grass border is already at {x0:g},{z0:g}','UNSUPPORTED_BORDER')
                require(np.ptp(poly[:,1])<1e-4 and abs(top-(height-OVERLAY_RISE))<=1e-4 and (texture in GROUND or 'yuka' in texture),
                        f'Border pieces need complete flat ordinary ground {OVERLAY_RISE} units below ({texture or "untextured"} at {x0:g},{z0:g})',
                        'UNSUPPORTED_BORDER')
                ground+=projected
        require(abs(ground-(x1-x0)*(z1-z0))<0.01,f'Border piece at {x0:g},{z0:g} is not over complete flat ground','UNSUPPORTED_BORDER')
        corners=[(x0,z0),(x1,z0),(x1,z1),(x0,z1)];order=[0,1,2,3]
        reference=donor.vertices[donor.polygons[0]][:,[0,2]]
        signed=lambda pts:sum(a[0]*b[1]-b[0]*a[1] for a,b in zip(pts,list(pts[1:])+[pts[0]]))
        if (signed(corners)>0)!=(signed([tuple(v) for v in reference])>0):order.reverse()
        tri=donor.triangles[0]
        quad=np.zeros((4,10))
        quad[:,0]=[corners[i][0] for i in order];quad[:,1]=height;quad[:,2]=[corners[i][1] for i in order]
        quad[:,3:5]=uv[order]
        quad[:,5:8]=donor.colors[tri[0]];quad[:,8]=donor.normals[tri[0]];quad[:,9]=donor.shade_commands[tri[0]]
        polys[donor_id].append(quad);boxes[donor_id].append(_box(quad));changed.add(donor_id)
    return _rewrite(raw,tileset,summary,bo,mo,model,shape,draws,by_shape,polys,changed)
