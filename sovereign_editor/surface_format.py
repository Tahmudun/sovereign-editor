"""Bounded flat-face replacement, preserving unedited Nitro sections and display lists."""
import struct
import numpy as np
import ndspy.model
from sovereign_editor import nitro
from sovereign_editor.formats import require


def layout(raw):
    blocks=nitro.blocks(raw)
    require(list(blocks)==[b'MDL0'], 'Surface writer needs a single MDL0 block')
    block=blocks[b'MDL0']; entries=nitro.info(block,8)
    require(len(entries)==1, 'Surface writer needs one model')
    bo=struct.unpack_from('<I',raw,16)[0]
    mo=struct.unpack('<I',entries[0][1])[0]
    model=block[mo:]; size,sbc,mat,shape,end=struct.unpack_from('<5I',model)
    require(size==len(model), 'Trailing model bytes')
    draws=nitro._draw_order(model,sbc,mat,nitro._nodes(model))
    return bo,mo,model,shape,draws


def split(poly, axis, edge, less):
    yes=[];no=[]
    for a,b in zip(poly,poly[1:]+poly[:1]):
        da=(a[axis]-edge)*(1 if less else -1);db=(b[axis]-edge)*(1 if less else -1)
        (yes if da<=0 else no).append(a)
        if (da<0<db) or (db<0<da):
            p=a+(b-a)*(-da/(db-da));yes.append(p);no.append(p)
    return yes,no


def pieces(poly,rect):
    remaining=poly; outside=[]
    for axis,edge,less in [(0,rect[0],False),(0,rect[2],True),(2,rect[1],False),(2,rect[3],True)]:
        if len(remaining)<3:break
        remaining,part=split(remaining,axis,edge,less)
        if len(part)>=3:outside.append(part)
    return outside,remaining


def tris(poly):
    result=[]
    for i in range(1,len(poly)-1):
        t=np.array([poly[0],poly[i],poly[i+1]])
        if np.linalg.norm(np.cross(t[1,:3]-t[0,:3],t[2,:3]-t[0,:3]))>1e-5:result.append(t)
    return result


def counts(raw):
    cursor=0;mode=None;n=0;t=q=0
    while cursor<len(raw):
        cmds=raw[cursor:cursor+4];cursor+=4
        for c in cmds:
            count=ndspy.model._COMMANDS_BY_ID[c].PARAM_COUNT
            value=struct.unpack_from('<I',raw,cursor)[0] if count else 0;cursor+=count*4
            if c==0x40:mode=value;n=0
            elif 0x23<=c<=0x28:n+=1
            elif c==0x41:
                if mode==0:t+=n//3
                elif mode==1:q+=n//4
                elif mode==2:t+=n-2
                else:q+=n//2-1
    return t,q


def encode(triangles, matrix, scale, width,height):
    require(triangles, 'Cannot empty a material shape')
    commands=[(0x40,struct.pack('<I',0))]
    inv=np.linalg.inv(matrix);last_color=last_normal=last_uv=None
    for tri in triangles:
        for v in tri:
            xyz=(inv@np.r_[v[:3],1])[:3]/scale
            coords=np.rint(xyz*4096).astype(int)
            require(np.all(coords>=-32768) and np.all(coords<=32767),'Vertex outside VTX_16 range')
            uv=np.rint(v[3:5]*[width,height]*16).astype(int)
            require(np.all(uv>=-32768) and np.all(uv<=32767),'UV outside TEXCOORD range')
            col=np.clip(np.rint(v[5:8]*31),0,31).astype(int)
            color=int(col[0]|col[1]<<5|col[2]<<10);normal=int(v[8]);uv=tuple(int(x) for x in uv)
            if color!=last_color:commands.append((0x20,struct.pack('<I',color)));last_color=color
            if normal!=last_normal:commands.append((0x21,struct.pack('<I',normal)));last_normal=normal
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


def paint(raw,tileset,rect,height,donor_shape,storage_base=None):
    summary,prims=nitro.decode_model(raw,tileset=tileset)
    bo,mo,model,shape,draws=layout(raw)
    require(all(np.allclose(m,np.eye(4)) for _,_,m in draws),'Surface writing requires identity node transforms')
    by_shape={sid:p for (sid,_,_),p in zip(draws,prims)}
    prims=[by_shape[i] for i in range(len(prims))]
    donor=prims[donor_shape]
    require(donor.material['texture_name'] and all(donor.material['repeat']),'Donor must repeat both axes')
    dtri=next((t for t in donor.triangles if np.max(np.abs(donor.vertices[t,1]-height))<1e-4),None)
    require(dtri is not None, 'Donor needs a flat face at target height')
    v=donor.vertices[dtri]
    affine=np.linalg.solve(np.column_stack([v[:,0],v[:,2],np.ones(3)]),donor.uvs[dtri])
    before=[];after=[];area=0;covered=0;changed=set()
    for sid,p in enumerate(prims):
        arr=np.column_stack([p.vertices,p.uvs,p.colors,p.normals]);bt=[arr[t].copy() for t in p.triangles];at=[]
        for tri in bt:
            outside,inside=pieces(list(tri),rect)
            inner=tris(inside) if len(inside)>=3 else []
            projected=sum(abs(np.cross(t[1,:3]-t[0,:3],t[2,:3]-t[0,:3])[1])/2 for t in inner)
            if projected < 1e-5:
                at.append(tri);continue
            require(np.max(tri[:,1])<=height+1e-4,
                    'This surface is covered by raised terrain, edging or baked scenery', 'UNSUPPORTED_SURFACE')
            if np.max(np.abs(tri[:,1]-height))>1e-4:
                at.append(tri);continue
            name=p.material['texture_name'] or ''
            require(name in ('grass01gs','grass02','road01','road01_r','road01_sub','grass02_r') or 'yuka' in name,
                    'This face is not an ordinary ground or floor surface', 'UNSUPPORTED_SURFACE')
            covered+=projected
            if sid==donor_shape:
                at.append(tri);continue
            require(len(set(tri[:,8]))==1,'Interpolated normal not supported')
            changed.add(sid)
            at.extend(t for part in outside for t in tris(part))
            for t in inner:
                area+=abs(np.cross(t[1,:3]-t[0,:3],t[2,:3]-t[0,:3])[1])/2
                t[:,3:5]=np.column_stack([t[:,0],t[:,2],np.ones(3)])@affine
                t[:,5:8]=donor.colors[dtri[0]];t[:,8]=donor.normals[dtri[0]]
                before.append(t)
        after.append(at)
    require(abs(covered-(rect[2]-rect[0])*(rect[3]-rect[1]))<0.01,
            'Choose tiles with one complete flat surface; this patch includes a gap, overlap or height change', 'UNSUPPORTED_SURFACE')
    if not changed:return raw,{'changed_shapes':[],'area':0,'before_bytes':len(raw),'after_bytes':len(raw)}
    after[donor_shape].extend(before);changed.add(donor_shape)
    # Rebuild from the immutable storage prefix each time, retaining just one
    # current display list per edited shape. Intermediate paint passes are never
    # appended to the final model. Unedited/unknown bytes of the baseline stay exact.
    storage_base=raw if storage_base is None else storage_base
    base_bo,base_mo,base_model,base_shape,base_draws=layout(storage_base)
    require((base_bo,base_mo,base_shape)==(bo,mo,shape),'Surface model layout changed')
    result=bytearray(base_model);old_t=old_q=new_t=0
    shapes=nitro.info(model,shape);base_shapes=nitro.info(base_model,shape)
    require(shapes==base_shapes,'Surface shape dictionary changed')
    for sid,mid,matrix in draws:
        p=shape+struct.unpack('<I',shapes[sid][1])[0]
        dl,length=struct.unpack_from('<2I',model,p+8)
        current=model[p+dl:p+dl+length]
        original_dl,original_length=struct.unpack_from('<2I',base_model,p+8)
        original=base_model[p+original_dl:p+original_dl+original_length]
        if sid in changed:
            t,q=counts(current);old_t+=t;old_q+=q
            encoded=encode(after[sid],matrix,summary['position_scale'],prims[sid].material['width'],prims[sid].material['height'])
            new_t+=len(after[sid])
        elif current!=original:encoded=current
        else:continue
        offset=len(result);result.extend(encoded)
        struct.pack_into('<II',result,p+8,offset-p,len(encoded))
    vertex=sum(len(after[i])*3 if i in changed else len(p.vertices) for i,p in enumerate(prims))
    oldv,surf,t,q=struct.unpack_from('<4H',model,36)
    t=t-old_t+new_t;q-=old_q
    require(max(vertex,t+q,t,q)<=65535,'Surface geometry count exceeds Nitro metadata','RESOURCE_CAPACITY')
    struct.pack_into('<4H',result,36,vertex,t+q,t,q)
    struct.pack_into('<I',result,0,len(result));struct.pack_into('<I',result,16,len(result))
    output=bytearray(raw[:bo+mo])+result
    struct.pack_into('<I',output,bo+4,len(output)-bo);struct.pack_into('<I',output,8,len(output))
    desc,read=nitro.decode_model(bytes(output),tileset=tileset)
    return bytes(output),{'changed_shapes':sorted(changed),'area':area/256,'before_bytes':len(raw),'after_bytes':len(output),'vertices':desc['vertices']}
