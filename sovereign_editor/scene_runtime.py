"""Opt-in targeted Poké Ball field effect for the pinned runtime.

Pure candidate bytes, composed with character bindings. Project owns writes.
The stock 853-command table, its count and all existing overlay bytes remain intact.
"""
import hashlib
import json
import struct
from pathlib import Path
from . import character_runtime as cr
from .formats import digest, immutable_digest, require, span

COLLECT_OPCODE=853
TABLE=0x020fad00
TABLE_POINTER=0x020400e4
COUNT=0x020fac90
ARM_BASE=0x02000000


def image(address):
    root=Path(__file__).with_name('native')
    value=json.loads((root/'scene_collect.json').read_text())
    require(hashlib.sha256((root/'scene_collect.c').read_bytes()).hexdigest()==value['source_sha256'],
            'Rebuild the scene collection runtime after source changes','BEFORE_VALUE_MISMATCH')
    data=bytearray.fromhex(value['code'])
    for offset,target in value['relocations']:
        addend=struct.unpack_from('<I',data,offset)[0]
        struct.pack_into('<I',data,offset,address+target+addend)
    return bytes(data),{key:address+v for key,v in value['symbols'].items()}


# RunScriptCommand (ARM9): an opcode at or above ctx->cmdCount reaches
# `bl GF_AssertFail` at DISPATCH_FAIL; valid opcodes use ctx->cmdTable.
DISPATCH_FAIL=0x0203fdbc
ASSERT_FAIL=0x0202551c
DISPATCH_LOOP=0x0203fda2
DISPATCH_TRUE=0x0203fdd4


def dispatch_hook(address,handler):
    """Thumb shim for the out-of-range branch: opcode 853 runs the collection
    command, every other opcode tail-calls GF_AssertFail with the original lr.

    r1 is the opcode and r4 the ScriptContext, as at DISPATCH_FAIL. The shim
    lives in the always-resident ARM9 extension (overlay 129); the stock table
    and count stay in ARM9, so no command depends on field-overlay memory.
    """
    require(address%4==0,'Dispatch shim must be word aligned')
    code=struct.pack('<14H',0x4806,0x4281,0xd108,0x4620,0x4905,0x4788,0x2801,0xd001,
                     0x4904,0x4708,0x4904,0x4708,0x4804,0x4700)
    return code+struct.pack('<5I',COLLECT_OPCODE,handler|1,DISPATCH_LOOP|1,DISPATCH_TRUE|1,ASSERT_FAIL|1)


def bindings(blob,plan,layout=None):
    """Collection code stays field-only (overlay 131); dispatch stays resident.

    r34/r37 relocated the whole command table into overlay 131. After an
    ordinary trainer loss the encounter task returns to the script without
    restoring the field, overlay 130 occupies that memory, and check_battle_won
    dispatched through battle-overlay bytes (GAMEPLAY-LOSS-001). The ARM9 table
    pointer and count are no longer changed.
    """
    require(immutable_digest(blob)==cr.BASELINE,'Collection requires the pinned baseline','UNSUPPORTED_RUNTIME')
    field=cr.overlay(blob,131)
    data=bytearray(plan['files'].get(field['file_id'],field['data']))
    data.extend(b'\0'*(-len(data)%4))
    code_address=field['address']+len(data)
    code,symbols=image(code_address);data.extend(code)
    # Same conservative reservation as character_runtime, even though the engine
    # linker reserves 0x18000. Never enter the adjacent overlay memory region.
    require(len(data)<=cr.FIELD_LIMIT,'Scene runtime exceeds reserved memory','RESOURCE_CAPACITY')
    resident=cr.overlay(blob,129)
    if layout is None:
        ext=bytearray(plan['files'].get(resident['file_id'],resident['data']))
        ext.extend(b'\0'*(-len(ext)%4))
        shim=resident['address']+len(ext)
        ext.extend(dispatch_hook(shim,symbols['collect_command']))
        require(len(ext)<=0x8000,'Scene runtime exceeds the ARM9 extension reservation','RESOURCE_CAPACITY')
    else:
        shim=layout.place('scene.dispatch',len(dispatch_hook(0,0)),4,'code',called_from=DISPATCH_FAIL)
        layout.write(shim,dispatch_hook(shim,symbols['collect_command']))
    arm_start=struct.unpack_from('<I',blob,0x20)[0]
    def patch(address,before,after,label):
        offset=arm_start+address-ARM_BASE
        require(span(blob,offset,len(before))==before,'Scene runtime before-value differs: '+label,'BEFORE_VALUE_MISMATCH')
        plan['patches'].append(dict(rom_offset=offset,before=before.hex(),after=after.hex(),kind=label))
    patch(DISPATCH_FAIL,cr.thumb_bl(DISPATCH_FAIL,ASSERT_FAIL),cr.thumb_bl(DISPATCH_FAIL,shim),'scene.dispatch-hook')
    sized=[(field,data,'scene.overlay-size')]+([(resident,ext,'scene.extension-size')] if layout is None else [])
    for info,payload,kind in sized:
        plan['files'][info['file_id']]=bytes(payload)
        offset=info['table_offset']+8
        existing=next((p for p in plan['patches'] if p['rom_offset']==offset),None)
        if existing:existing['after']=struct.pack('<I',len(payload)).hex()
        else:plan['patches'].append(dict(rom_offset=offset,before=span(blob,offset,4).hex(),
                                       after=struct.pack('<I',len(payload)).hex(),kind=kind))
    return plan
