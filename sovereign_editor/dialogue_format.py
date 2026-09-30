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
    # [mon] is the stock party-nickname buffer 0 (text bank 211 "{STRVAR_1 1, 0}
    # used Cut!": FFFE 0100, two arguments 0 0); it is budgeted as ten characters.
    parts=re.split(r'(\[/?hint\]|\[mon\])',text);visible='';codes=[];highlight=False
    for part in parts:
        if part=='[mon]':
            visible+='M'*10;codes.extend((0xfffe,0x0100,2,0,0))
        elif part=='[hint]':
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
        chars=(encode_pages(text,wait=isinstance(text,Held)) if isinstance(text,list) else encode_message(text));offset=len(result)
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


def append_scripts(raw,scripts,align=None):
    """Append script bodies after the stock ones.

    align gives each script's required start alignment (default 1). Movement data
    is 4-aligned inside a compiled scene script, like native `.balign 4`, so that
    script must also start 4-aligned in the file: the ARM9 reads movement commands
    with LDRH, which ignores bit 0 of an odd address. Entries grow in 4-byte steps,
    so earlier bodies keep their alignment.
    """
    if not scripts:return raw
    align=[1]*len(scripts) if align is None else list(align)
    require(len(align)==len(scripts) and all(a in (1,2,4) for a in align),'Invalid script alignment')
    end,entries=script_entries(raw);extra=4*len(scripts)
    require(len(entries)+len(scripts)<1000,'Local script IDs exhausted','UNSUPPORTED_SCRIPT')
    result=bytearray(b'\0'*(4*(len(entries)+len(scripts)))+b'\x13\xfd'+raw[end:])
    targets=[offset+extra for offset in entries]
    for code,a in zip(scripts,align):
        result.extend(b'\0'*(-len(result)%a))
        targets.append(len(result));result.extend(code)
    for i,target in enumerate(targets):struct.pack_into('<I',result,4*i,target-4*i-4)
    new_end,new_entries=script_entries(result)
    require(result[new_end:new_end+len(raw)-end]==raw[end:] and new_entries==targets,
            'Stock script body or targets changed')
    return bytes(result)


PAGE_BREAK = 0x25BC   # stock trainer text (bank 728): wait, then continue in a fresh box


class Held(list):
    """Pages whose last page also waits for a button, as every stock trainer intro does
    (bank 728 types 0/3/7 end with PAGE_BREAK): the battle starts only after the press."""


def encode_pages(pages, wait=False):
    """One native message of several authored pages (each checked like a single page)."""
    require(isinstance(pages, list) and 1 <= len(pages) <= 4, 'A message needs 1..4 pages', 'INVALID_INPUT')
    codes = []
    for i, page in enumerate(pages):
        if i:
            codes.append(PAGE_BREAK)
        codes.extend(encode_message(page)[:-1])
    return codes + ([PAGE_BREAK] if wait else []) + [0xffff]


def alias_scripts(raw, count, source=0):
    """Extend the entry table to ``count`` entries whose new members run entry ``source``.

    Existing entries keep their targets; bodies move by the table growth (a multiple of
    four, so movement-data alignment is kept; script jumps are relative).
    """
    end, entries = script_entries(raw)
    if count <= len(entries):
        return raw
    require(count < 1000, 'Local script IDs exhausted', 'UNSUPPORTED_SCRIPT')
    extra = 4 * (count - len(entries))
    result = bytearray(b'\0' * (4 * count) + b'\x13\xfd' + raw[end:])
    targets = [offset + extra for offset in entries] + [entries[source] + extra] * (count - len(entries))
    for i, target in enumerate(targets):
        struct.pack_into('<I', result, 4 * i, target - 4 * i - 4)
    new_end, new_entries = script_entries(bytes(result))
    require(bytes(result[new_end:]) == raw[end:] and new_entries == targets, 'Stock script body or targets changed')
    return bytes(result)
