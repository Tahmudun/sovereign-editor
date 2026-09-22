"""Decode the supported runtime assets for validation and frame-by-frame review."""
import struct
from PIL import Image
from .formats import require, span


def blocks(raw):
    magic,bom,version,size,header,count=struct.unpack('<4sHHIHH',span(raw,0,16))
    require(bom==0xfeff and header==16 and size==len(raw) and 1<=count<=4, 'Invalid trainer container', 'INVALID_CHARACTER')
    result={};at=16
    for _ in range(count):
        name,length=struct.unpack('<4sI',span(raw,at,8))
        require(name not in result and length>=8,'Invalid trainer block','INVALID_CHARACTER')
        result[name]=span(raw,at+8,length-8);at+=length
    require(at==len(raw),'Unexpected trainer bytes','INVALID_CHARACTER')
    return result


def trainer_frames(parts):
    from .character_runtime import decoded
    bs=[blocks(decoded(p)) for p in parts]
    require(all(k in b for k,b in zip((b'RAHC',b'TTLP',b'KBEC',b'KNBA',b'RAHC'),bs)), 'Missing trainer block','INVALID_CHARACTER')
    gfx=bs[0][b'RAHC'];h,w,depth,u1,u2,flag,size,offset=struct.unpack('<HHIHHIII',span(gfx,0,24))
    require(depth==3 and flag==256 and size in (3200,16000), 'Unsupported trainer pixel layout','INVALID_CHARACTER')
    pixels=span(gfx,offset,size)
    pal=bs[1][b'TTLP'];pd,unknown,ps,po=struct.unpack('<4I',span(pal,0,16))
    colors=[((v&31)*255//31,((v>>5)&31)*255//31,((v>>10)&31)*255//31,0 if i==0 else 255)
            for i,(v,) in enumerate(struct.iter_unpack('<H',span(pal,po,32)))]
    cells=bs[2][b'KBEC'];n,attr,co,mapping,vo,unused,ue=struct.unpack('<HH5I',span(cells,0,24))
    require(n in (1,5) and attr==1 and mapping==1 and vo>0, 'Unsupported trainer cells','INVALID_CHARACTER')
    maxsize,ve=struct.unpack('<2I',span(cells,vo,8));require(maxsize==3200,'Invalid trainer cell buffer','INVALID_CHARACTER')
    dims=(((8,8),(16,16),(32,32),(64,64)),((16,8),(32,8),(32,16),(64,32)),((8,16),(8,32),(16,32),(32,64)))
    frames=[]
    for i in range(n):
        frame=Image.new('RGBA',(96,80));covered=set()
        no,ca,oo=struct.unpack('<HHI',span(cells,co+i*16,8));require(1<=no<=16,'Invalid OAM count','INVALID_CHARACTER')
        base,length=struct.unpack('<2I',span(cells,vo+ve+i*8,8))
        require(length==3200 and base+length<=len(pixels), 'Trainer VRAM transfer exceeds graphics','INVALID_CHARACTER')
        for j in range(no):
            a0,a1,a2=struct.unpack('<3H',span(cells,co+n*16+oo+j*6,6))
            shape=a0>>14;scale=a1>>14
            require(shape<3 and not a0&0x2100 and not a2&0xf000, 'Unsupported trainer OAM format','INVALID_CHARACTER')
            width,height=dims[shape][scale];x=a1&511;y=a0&255
            if x>=256:x-=512
            if y>=128:y-=256
            piece=Image.new('RGBA',(width,height))
            tile=(a2&1023)*2
            for py in range(height):
                for px in range(width):
                    idx=tile+(py//8)*(width//8)+px//8
                    at=base+idx*32+(py%8)*4+(px%8)//2
                    require(base<=at<base+length,'Trainer OAM exceeds its cell','INVALID_CHARACTER')
                    v=(pixels[at]>>(4*(px%2)))&15;piece.putpixel((px,py),colors[v])
            if a1&0x1000:piece=piece.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
            if a1&0x2000:piece=piece.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
            require(0<=x+40 and x+40+width<=96 and 0<=y+40 and y+40+height<=80,'Trainer OAM outside supported battle canvas','INVALID_CHARACTER')
            for py in range(y+40,y+40+height):
                for px in range(x+40,x+40+width):
                    require((px,py) not in covered,'Overlapping trainer cells are unsupported','INVALID_CHARACTER');covered.add((px,py))
            frame.paste(piece,(x+40,y+40))
        require(len(covered)==6400,'Trainer cell does not cover the full canvas','INVALID_CHARACTER');frames.append(frame)
    anim=bs[3][b'KNBA'];ns,nf,so,fo,do=struct.unpack('<HH3I',span(anim,0,16));sequences=[]
    require(1<=ns<=8 and nf<=128,'Invalid animation capacity','INVALID_CHARACTER')
    for i in range(ns):
        count,loop,element,atype,mode,fp=struct.unpack('<4H2I',span(anim,so+16*i,16))
        require(1<=count<=128 and loop<count and element==0 and atype==1 and mode in (1,2), 'Unsupported trainer animation','INVALID_CHARACTER')
        sequence=[]
        for k in range(count):
            dp,delay,pad=struct.unpack('<I2H',span(anim,fo+fp+8*k,8));cell=struct.unpack('<H',span(anim,do+dp,2))[0]
            require(cell<n and 1<=delay<=600,'Animation references invalid cell/timing','INVALID_CHARACTER');sequence.append((cell,delay))
        sequences.append(sequence)
    require(sum(map(len,sequences))==nf,'Animation frame count differs','INVALID_CHARACTER')
    enc=bs[4][b'RAHC']
    require(struct.unpack('<HHIHHIII',span(enc,0,24))==(10,20,3,0,0,1,6400,24) and len(enc)==6424,
            'Unsupported encoded trainer picture','INVALID_CHARACTER')
    return frames,sequences


def overworld_frames(package):
    from .character_runtime import decoded
    from .nitro import texture_set
    import ndspy.texture
    textures,palettes=texture_set(decoded(package['overworld']));palette=[ndspy.color.pack(*c) for c in next(iter(palettes.values()))]
    result=[]
    for name,texture in sorted(textures.items(),key=lambda kv:int(kv[0].rsplit('.',1)[1])):
        require(texture.width==texture.height==32,'Overworld frames must be 32×32','INVALID_CHARACTER')
        rgba=ndspy.texture.renderTextureDataAsImage(texture.data1,texture.data2,texture.format,texture.width,texture.height,palette,
                                                   texture.isColor0Transparent)
        result.append(rgba)
    return result
