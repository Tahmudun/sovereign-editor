"""Append isolated plain messages and talk/read scripts; keep stock bodies exact.

HGSS layout: pinned pret/pokeheartgold src/msgdata.c, asm/macros/script.inc;
DSPRE ScriptFile.cs header serializer and charmap. See area-authoring evidence.
"""
import json
import re
import struct
from pathlib import Path
from .formats import require, span

CHARS = json.loads((Path(__file__).parent/'assets/dialogue-chars.json').read_text())
SCRIPT_ARCHIVE='a/0/1/2'
TEXT_ARCHIVE='a/0/2/7'


def text_entries(raw):
    count,key=struct.unpack('<HH',span(raw,0,4)); entries=[]
    for i in range(count):
        seed=key*765*(i+1)&65535; mask=seed|(seed<<16)
        offset,length=(v^mask for v in struct.unpack('<II',span(raw,4+8*i,8)))
        require(offset>=4+8*count and length>0, 'Invalid message allocation')
        data=span(raw,offset,length*2)
        entries.append((offset,length,data))
    return key,entries


def encode_message(text):
    require(isinstance(text,str) and 1<=len(text)<=200, 'Dialogue needs a short page', 'INVALID_INPUT')
    # A bounded semantic span, not an arbitrary control-code escape. Native
    # FF00 color index 1 uses the existing field palette; reset to index 0.
    parts=re.split(r'(\[/?hint\])',text);visible='';codes=[];highlight=False
    for part in parts:
        if part=='[hint]':
            require(not highlight,'Hint spans cannot nest','INVALID_INPUT');highlight=True
            codes.extend((0xfffe,0xff00,1,1))
        elif part=='[/hint]':
            require(highlight,'Hint closing tag has no opening tag','INVALID_INPUT');highlight=False
            codes.extend((0xfffe,0xff00,1,0))
        else:
            part=part.replace("'",'’')
            require(all(c in CHARS for c in part), 'Unsupported character in dialogue', 'INVALID_INPUT')
            visible+=part;codes.extend(CHARS[c] for c in part)
    require(not highlight,'Close the hint span','INVALID_INPUT')
    require(bool(visible.strip()) and len(visible)<=57, 'Dialogue needs 1..57 visible characters', 'INVALID_INPUT')
    require(len(visible.split('\n'))<=2 and all(len(s)<=28 for s in visible.split('\n')),
            'Use at most 2 lines of 28 characters; insert a line break', 'INVALID_INPUT')
    return codes+[0xffff]


def append_messages(raw,messages,limit=256):
    if not messages:return raw
    key,entries=text_entries(raw);count=len(entries);extra=8*len(messages)
    require(count+len(messages)<=limit, 'Message bank capacity exceeded', 'UNSUPPORTED_TEXT')
    end=4+8*count
    result=bytearray(struct.pack('<HH',count+len(messages),key)+b'\0'*(8*(count+len(messages)))+raw[end:])
    allocations=[(offset+extra,length) for offset,length,_ in entries]
    for i,text in enumerate(messages,count):
        chars=encode_message(text);offset=len(result)
        cipher=[c^(((i+1)*596947+j*18749)&65535) for j,c in enumerate(chars)]
        result.extend(struct.pack('<'+'H'*len(cipher),*cipher));allocations.append((offset,len(chars)))
    for i,(offset,length) in enumerate(allocations):
        seed=key*765*(i+1)&65535;mask=seed|seed<<16
        struct.pack_into('<II',result,4+i*8,offset^mask,length^mask)
    _,read=text_entries(result)
    require(all(a[2]==b[2] for a,b in zip(entries,read)),'Stock encrypted messages changed')
    return bytes(result)


def script_entries(raw):
    cursor=0;entries=[]
    while struct.unpack('<H',span(raw,cursor,2))[0]!=0xfd13:
        offset=struct.unpack('<I',span(raw,cursor,4))[0]+cursor+4
        require(offset<len(raw),'Invalid script entry target')
        entries.append(offset);cursor+=4
        require(len(entries)<1000,'Script entry table exceeds local-script ID range')
    require(all(offset>=cursor+2 for offset in entries),'Script entry overlaps table')
    return cursor+2,entries


def talk_script(message,kind):
    require(type(message) is int and 0<=message<=255,'Unsupported simple message index')
    require(kind in ('npc','background'),'Unsupported simple interaction')
    # Same bounded sequence as the stock Cherrygrove resident: SE/select, lock,
    # face player (NPC only), message, wait button, close, release, end.
    code=struct.pack('<3H',73,1500,96)
    if kind=='npc':code+=struct.pack('<H',104)
    return code+struct.pack('<HB4H',45 if kind=='npc' else 44,message,50,53,97,2)


def append_scripts(raw,scripts):
    if not scripts:return raw
    end,entries=script_entries(raw);extra=4*len(scripts)
    require(len(entries)+len(scripts)<1000,'Local script IDs exhausted','UNSUPPORTED_SCRIPT')
    result=bytearray(b'\0'*(4*(len(entries)+len(scripts)))+b'\x13\xfd'+raw[end:])
    targets=[offset+extra for offset in entries]
    for code in scripts:
        targets.append(len(result));result.extend(code)
    for i,target in enumerate(targets):struct.pack_into('<I',result,4*i,target-4*i-4)
    new_end,new_entries=script_entries(result)
    require(result[new_end:new_end+len(raw)-end]==raw[end:] and new_entries==targets,
            'Stock script body or targets changed')
    return bytes(result)
