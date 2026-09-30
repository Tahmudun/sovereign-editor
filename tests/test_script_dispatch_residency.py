"""GAMEPLAY-LOSS-001: script dispatch must not depend on field-overlay memory.

Runs the pinned ARM9 RunScriptCommand/ScriptReadHalfword on a CPU. Command
handlers, GF_AssertFail and the collection command are modeled leaves; the
overlay placement and dispatch path are the real exported bytes.
"""
import struct
import sys
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'work/tiana-fixes-1/python-tools'))
pytest.importorskip('unicorn')
from unicorn import Uc,UcError,UC_ARCH_ARM,UC_MODE_THUMB,UC_HOOK_CODE
from unicorn.arm_const import UC_ARM_REG_R0,UC_ARM_REG_SP,UC_ARM_REG_LR,UC_ARM_REG_PC
from sovereign_editor import character_runtime as cr, scene_runtime as sr
from sovereign_editor.formats import arm9_code

BASELINE=ROOT/'projects/gameplay-data-1/baseline.nds'
R37=ROOT/'projects/gameplay-data-1/exports/route30-r37/game.nds'
RUN=0x0203fd6c;STOP=0x02001000;CTX=0x022b0000;SCRIPT=0x022b1000
CHECK_WON=0x020498d4;WHITE_OUT=0x020498c8


def rom(path):
    if not path.exists():pytest.skip('Pinned ROM is local and untracked')
    return path.read_bytes()


class Machine:
    def __init__(self,blob,files=None,post_loss=True,arm=None):
        files=files or {}
        self.u=Uc(UC_ARCH_ARM,UC_MODE_THUMB);self.u.mem_map(0x02000000,0x400000)
        self.u.mem_write(0x02000000,arm or arm9_code(blob))
        for number in (131,129):
            info=cr.overlay(blob,number);self.u.mem_write(info['address'],files.get(info['file_id'],info['data']))
        if post_loss:
            # Battle extension shares RAM with the field extension; the field is
            # not restored before a lost trainer battle returns to its script.
            battle=cr.overlay(blob,130);self.u.mem_write(battle['address'],battle['data'])
        self.calls=[];self.leaves={}
        self.u.hook_add(UC_HOOK_CODE,self.leaf)
    def leaf(self,u,address,size,_):
        if address not in self.leaves:return
        name,result,consume=self.leaves[address];self.calls.append(name)
        ptr=struct.unpack('<I',u.mem_read(CTX+8,4))[0];u.mem_write(CTX+8,struct.pack('<I',ptr+consume))
        u.reg_write(UC_ARM_REG_R0,result);u.reg_write(UC_ARM_REG_PC,u.reg_read(UC_ARM_REG_LR))
    def run(self,table,count,script):
        ctx=bytearray(0x84);ctx[1]=1
        struct.pack_into('<I',ctx,8,SCRIPT);struct.pack_into('<2I',ctx,0x5c,table,count)
        self.u.mem_write(CTX,bytes(ctx));self.u.mem_write(SCRIPT,struct.pack(f'<{len(script)}H',*script))
        self.u.reg_write(UC_ARM_REG_R0,CTX);self.u.reg_write(UC_ARM_REG_SP,0x02390000);self.u.reg_write(UC_ARM_REG_LR,STOP|1)
        self.u.emu_start(RUN|1,STOP,count=5000)
        assert self.u.reg_read(UC_ARM_REG_PC)==STOP,'Dispatcher did not return'
        return self.u.reg_read(UC_ARM_REG_R0),self.u.mem_read(CTX+1,1)[0]


def repaired(blob):
    plan=sr.bindings(blob,{'files':{},'patches':[],'appends':{},'characters':[]})
    data=bytearray(blob)
    for p in plan['patches']:
        after=bytes.fromhex(p['after']);data[p['rom_offset']:p['rom_offset']+len(after)]=after
    return bytes(data),plan


def test_r37_layout_dispatches_battle_overlay_bytes_after_loss():
    blob=rom(R37);arm=arm9_code(blob)
    table=struct.unpack_from('<I',arm,sr.TABLE_POINTER-sr.ARM_BASE)[0]
    count=struct.unpack_from('<I',arm,sr.COUNT-sr.ARM_BASE)[0]
    assert (table,count)==(0x023cf340,854)
    m=Machine(blob);handler=struct.unpack('<I',m.u.mem_read(table+220*4,4))[0]
    assert handler==0x42aa3201 and handler!=CHECK_WON|1
    with pytest.raises(UcError):m.run(table,count,[220,0x800c,2])


def test_repaired_dispatch_keeps_stock_table_and_resident_collect_shim():
    blob=rom(BASELINE);patched,plan=repaired(blob);arm=arm9_code(patched)
    assert struct.unpack_from('<I',arm,sr.TABLE_POINTER-sr.ARM_BASE)[0]==sr.TABLE
    assert struct.unpack_from('<I',arm,sr.COUNT-sr.ARM_BASE)[0]==sr.COLLECT_OPCODE
    assert arm9_code(blob)[sr.TABLE-sr.ARM_BASE:sr.TABLE-sr.ARM_BASE+853*4]==arm[sr.TABLE-sr.ARM_BASE:sr.TABLE-sr.ARM_BASE+853*4]
    kinds=sorted(p['kind'] for p in plan['patches'])
    assert kinds==['scene.dispatch-hook','scene.extension-size','scene.overlay-size']
    field,resident=cr.overlay(blob,131),cr.overlay(blob,129)
    assert plan['files'][field['file_id']].startswith(field['data'])
    assert plan['files'][resident['file_id']].startswith(resident['data']) and len(plan['files'][resident['file_id']])<=0x8000
    code,symbols=sr.image(field['address']+len(field['data'])+(-len(field['data'])%4))
    collect=symbols['collect_command']&~1
    for post_loss in (True,False):
        m=Machine(blob,plan['files'],post_loss,arm)
        m.leaves={CHECK_WON:('check_battle_won',0,2),WHITE_OUT:('white_out',1,0),collect:('collect',1,2),0x0202551c:('assert',0,0)}
        # Loss branch: result check then blackout, both through the stock ARM9 table.
        assert m.run(sr.TABLE,853,[220,0x800c,219,97,2])==(1,1) and m.calls==['check_battle_won','white_out']
        if not post_loss:
            m.calls.clear();assert m.run(sr.TABLE,853,[220,0x800c,853,5])==(1,1)
            assert m.calls==['check_battle_won','collect']
        m.calls.clear();assert m.run(sr.TABLE,853,[900])==(0,0) and m.calls==['assert']


def test_repair_composes_after_character_runtime_and_refuses_other_baselines():
    blob=rom(BASELINE)
    from sovereign_editor.formats import EditorError
    with pytest.raises(EditorError):sr.bindings(blob[:-1]+bytes([blob[-1]^1]),{'files':{},'patches':[]})
    resident=cr.overlay(blob,129)
    # A prior character-runtime size patch for overlay 129 is updated, not duplicated.
    offset=resident['table_offset']+8
    prior={'files':{resident['file_id']:resident['data']+b'\1'*6},'patches':[{'rom_offset':offset,'before':'00','after':'00','kind':'character.overlay-size'}]}
    plan=sr.bindings(blob,prior)
    assert [p['kind'] for p in plan['patches'] if p['rom_offset']==offset]==['character.overlay-size']
    assert struct.unpack('<I',bytes.fromhex(plan['patches'][0]['after']))[0]==len(plan['files'][resident['file_id']])
