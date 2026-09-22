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


def sample_mapping(raw, tileset, x, z):
    """Native planar UV mapping at a local tile center, with no invented scaling."""
    _, prims = nitro.decode_model(raw, tileset=tileset)
    point = [((x + .5) * 16 - 256), ((z + .5) * 16 - 256), 1]
    candidates = []
    for p in prims:
        for tri in p.triangles:
            v = p.vertices[tri]
            a = np.column_stack([v[:, 0], v[:, 2], np.ones(3)])
            if abs(np.linalg.det(a)) < 1e-6:
                continue
            weights = np.linalg.solve(a.T, point)
            if np.min(weights) >= -1e-6:
                candidates.append((float(weights @ v[:, 1]), float(min(weights)), p, tri, a))
    require(candidates, 'No floor at this tile', 'UNSUPPORTED_SURFACE')
    height = max(c[0] for c in candidates)
    top = [c for c in candidates if abs(c[0] - height) < .01]
    require(len({c[2].material['name'] for c in top}) == 1, 'Ambiguous overlapping surfaces', 'UNSUPPORTED_SURFACE')
    _, _, p, tri, a = max(top, key=lambda c: c[1])
    require(np.ptp(p.vertices[tri, 1]) < 1e-4 and all(p.material['repeat']),
            'Sample a flat repeating floor', 'UNSUPPORTED_SURFACE')
    return {'material': p.material['name'], 'height': height,
            'affine': np.linalg.solve(a, p.uvs[tri]).round(12).reshape(-1).tolist()}


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
    commands=[];mode=None;last_shade=last_uv=None
    for poly in polygons:
        wanted=0 if len(poly)==3 else 1
        require(len(poly) in (3,4),'Invalid native polygon')
        if wanted!=mode:
            if mode is not None:commands.append((0x41,b''))
            commands.append((0x40,struct.pack('<I',wanted)));mode=wanted
        for v in poly:
            coords=np.rint(v[:3]/scale*4096).astype(int)
            require(np.all(coords>=-32768) and np.all(coords<=32767),'Vertex outside VTX_16 range')
            uv=np.rint(v[3:5]*[width,height]*16).astype(int)
            require(np.all(uv>=-32768) and np.all(uv<=32767),'UV outside TEXCOORD range')
            kind=int(v[9]);require(kind in (0x20,0x21),'Inherited lighting state is unsupported','UNSUPPORTED_SURFACE')
            if kind==0x20:
                col=np.clip(np.rint(v[5:8]*31),0,31).astype(int)
                value=int(col[0]|col[1]<<5|col[2]<<10)
            else:value=int(v[8])
            shade=(kind,value)
            if shade!=last_shade:commands.append((kind,struct.pack('<I',value)));last_shade=shade
            uv=tuple(int(x) for x in uv)
            if uv!=last_uv:commands.append((0x22,struct.pack('<2h',*uv)));last_uv=uv
            if np.all(coords%64==0) and np.all(coords//64>=-512) and np.all(coords//64<=511):
                value=sum((int(c//64)&1023)<<shift for c,shift in zip(coords,(0,10,20)))
                commands.append((0x24,struct.pack('<I',value)))
            else:commands.append((0x23,struct.pack('<3hH',*coords,0)))
    commands.append((0x41,b''));result=bytearray()
    for i in range(0,len(commands),4):
        packet=commands[i:i+4]
        result.extend(bytes(c for c,_ in packet)+bytes(4-len(packet)))
        for _,params in packet:result.extend(params)
    return bytes(result)


def build(raw,tileset,assignments):
    if not assignments:return raw
    summary,prims=nitro.decode_model(raw,tileset=tileset)
    bo,mo,model,shape,draws=legacy.layout(raw)
    require(all(np.allclose(m,np.eye(4)) for _,_,m in draws),'Surface writing requires identity node transforms')
    by_shape={sid:p for (sid,_,_),p in zip(draws,prims)}
    shapes={p.material['name']:sid for sid,p in by_shape.items()}
    polys={}
    for sid,p in by_shape.items():
        arr=np.column_stack([p.vertices,p.uvs,p.colors,p.normals,p.shade_commands])
        polys[sid]=[arr[indices].copy() for indices in p.polygons]
    changed=set()
    for assignment in assignments:
        x,z,material,height = assignment[:4]
        sampled = assignment[4] if len(assignment) > 4 else None
        donor_id=shapes[material];donor=by_shape[donor_id]
        require(donor.material['texture_name'] and all(donor.material['repeat']),'Donor must repeat both axes')
        dtri=next((t for t in donor.triangles if np.max(np.abs(donor.vertices[t,1]-height))<1e-4),None)
        require(dtri is not None,'Donor needs a flat face at target height')
        v=donor.vertices[dtri]
        affine=np.linalg.solve(np.column_stack([v[:,0],v[:,2],np.ones(3)]),donor.uvs[dtri])
        if sampled is not None:
            affine = np.asarray(sampled, dtype=float).reshape(3, 2)
        rect=[x*16-256,z*16-256,(x+1)*16-256,(z+1)*16-256]
        covered=0;added=[]
        for sid,p in by_shape.items():
            output=[]
            for poly in polys[sid]:
                outside,inside=legacy.pieces(list(poly),rect)
                projected=area(inside)
                if projected<1e-5:output.append(poly);continue
                require(np.max(poly[:,1])<=height+1e-4,
                        'This surface is covered by raised terrain, edging or baked scenery','UNSUPPORTED_SURFACE')
                if np.max(np.abs(poly[:,1]-height))>1e-4:output.append(poly);continue
                name=p.material['texture_name'] or ''
                require(name in ('grass01gs','grass02','road01','road01_r','road01_sub','grass02_r') or 'yuka' in name,
                        'This face is not an ordinary ground or floor surface','UNSUPPORTED_SURFACE')
                covered+=projected
                # Existing source faces keep their native mapping when painting
                # the same material. A sampled donor maps newly filled faces.
                if sid==donor_id:output.append(poly);continue
                require(len(set(poly[:,9]))==1 and (poly[0,9]!=0x21 or len(set(poly[:,8]))==1),
                        'Interpolated lighting normals are unsupported','UNSUPPORTED_SURFACE')
                changed.add(sid)
                output.extend(part for piece in outside for part in fragments(piece))
                for part in fragments(inside):
                    part[:,3:5]=np.column_stack([part[:,0],part[:,2],np.ones(len(part))])@affine
                    part[:,5:8]=donor.colors[dtri[0]];part[:,8]=donor.normals[dtri[0]]
                    part[:,9]=donor.shade_commands[dtri[0]];added.append(part)
            polys[sid]=output
        require(abs(covered-256)<0.01,
                'Choose tiles with one complete flat surface; this patch includes a gap, overlap or height change','UNSUPPORTED_SURFACE')
        if added:polys[donor_id].extend(added);changed.add(donor_id)
    if not changed:return raw
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
    require(max(vertices,t+q,t,q)<=65535,'Surface geometry exceeds Nitro metadata','RESOURCE_CAPACITY')
    struct.pack_into('<4H',result,36,vertices,t+q,t,q)
    struct.pack_into('<I',result,0,len(result));struct.pack_into('<I',result,16,len(result))
    output=bytearray(raw[:bo+mo])+result
    struct.pack_into('<I',output,bo+4,len(output)-bo);struct.pack_into('<I',output,8,len(output))
    require(len(output)<=0xf000,'Authored model exceeds the 60 KiB map buffer; use a smaller selection','RESOURCE_CAPACITY')
    nitro.decode_model(bytes(output),tileset=tileset)
    return bytes(output)
