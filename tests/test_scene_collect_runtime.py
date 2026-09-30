"""Run the compiled Thumb effect on a CPU; rendering/resource leaves are modeled.

This verifies dispatch, actor identity, timing and cleanup, not native visuals.
"""
import struct
import sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'work/tiana-fixes-1/python-tools'))
unicorn=pytest.importorskip('unicorn')
from unicorn import Uc,UC_ARCH_ARM,UC_MODE_THUMB,UC_HOOK_CODE
from unicorn.arm_const import UC_ARM_REG_R0,UC_ARM_REG_R1,UC_ARM_REG_R2,UC_ARM_REG_R3,UC_ARM_REG_SP,UC_ARM_REG_LR,UC_ARM_REG_PC
from sovereign_editor.scene_runtime import image

BASE=0x023d0000;STOP=0x02001000;CTX=0x022b0000;FIELD=0x022b1000
OBJ=0x022a2000;FOLLOWER=0x022a3000;SPRITE=0x022a4000
MANAGER=0x022b2000;RESOURCE=0x022b3000;VAR=0x022b6000;SLOT=0x022b7000
EFFECT=0x022a1000;WORK=0x022a1100;PAYLOAD=0x022a1200
REGS=[UC_ARM_REG_R0,UC_ARM_REG_R1,UC_ARM_REG_R2,UC_ARM_REG_R3]

class CPU:
    def __init__(self):
        self.u=Uc(UC_ARCH_ARM,UC_MODE_THUMB);self.u.mem_map(0x02000000,0x400000)
        code,self.symbols=image(BASE);self.u.mem_write(BASE,code)
        self.w(CTX+128,FIELD);self.w(FIELD+60,MANAGER)
        self.id=11;self.sprite_id=597;self.present=True;self.sprite=True;self.resource=True;self.resource_data=True;self.spawn=True;self.valid=True
        self.scales=[];self.hidden=[];self.active=[];self.destroyed=False;self.calls=[];self.spawned=False;self.native=None
        self.u.hook_add(UC_HOOK_CODE,self.stub)
    def w(self,a,v):self.u.mem_write(a,struct.pack('<I',v))
    def r(self,a):return struct.unpack('<I',self.u.mem_read(a,4))[0]
    def args(self):return [self.u.reg_read(r) for r in REGS]
    def call(self,name,*args):
        for r,v in zip(REGS,args):self.u.reg_write(r,v)
        self.u.reg_write(UC_ARM_REG_SP,0x02390000);self.u.reg_write(UC_ARM_REG_LR,STOP|1)
        self.u.emu_start(self.symbols[name],STOP,count=20000)
        assert self.u.reg_read(UC_ARM_REG_PC)==STOP,'Compiled function did not return'
        return self.u.reg_read(UC_ARM_REG_R0)
    def stub(self,u,address,size,_):
        if address>=BASE or address==STOP:return
        a,b,c,d=self.args();self.calls.append(address);value=0
        if address==0x0203fe2c:value=self.id
        elif address==0x02040374:assert (a,b)==(FIELD,0x800a);value=VAR
        elif address==0x0205ee60:assert (a,b)==(MANAGER,self.id);value=OBJ if self.present else 0
        elif address==0x0205f25c:assert a==OBJ;value=self.sprite_id
        elif address==0x021f72dc:assert a==OBJ;value=SPRITE if self.sprite else 0
        elif address==0x021f146c:assert a==OBJ;value=MANAGER
        elif address==0x021f1468:assert a==MANAGER;value=FIELD
        elif address==0x021f1588:
            assert (a,b)==(MANAGER,17);self.w(SLOT+4,RESOURCE if self.resource_data else 0);value=SLOT if self.resource else 0
        elif address==0x0205f944:assert a==OBJ;u.mem_write(b,struct.pack('<3i',100,20,200))
        elif address==0x0205f09c:assert (a,b)==(OBJ,2);value=7
        elif address==0x021f1620:
            assert a==MANAGER and d==1
            sp=u.reg_read(UC_ARM_REG_SP);payload=self.r(sp)
            assert self.r(sp+4)==7
            u.mem_write(PAYLOAD,bytes(u.mem_read(payload,40)))
            assert self.r(PAYLOAD+24)==OBJ and self.r(PAYLOAD+36)==CTX+100
            assert self.r(PAYLOAD+8)==200+6*4096
            self.descriptor=list(struct.unpack('<5I',u.mem_read(b,20)))
            self.spawned=self.spawn;value=EFFECT if self.spawn else 0
        elif address==0x0203fd58:assert a==CTX;self.native=b
        elif address==0x02068d98:assert a==EFFECT;value=PAYLOAD
        elif address==0x02203820:
            assert (a,b)==(EFFECT,WORK)
            u.mem_write(WORK+24,bytes(u.mem_read(PAYLOAD,36)))
            self.w(WORK,0);self.w(WORK+4,self.sprite_id);self.w(WORK+8,self.id);self.w(WORK+12,33);value=1
        elif address==0x0205f0f8:assert (a,b,c,d)==(OBJ,self.sprite_id,self.id,33);value=int(self.valid)
        elif address==0x02023e78:assert a==SPRITE;self.scales.append(struct.unpack('<3i',u.mem_read(b,12)))
        elif address==0x02069dc8:assert a==OBJ;self.hidden.append((a,b))
        elif address==0x021fc004:assert a==RESOURCE+0x3c;self.active.append(b)
        elif address==0x021f1640:assert a==EFFECT;self.destroyed=True
        else:raise AssertionError(f'Unexpected native call {address:#x}')
        u.reg_write(UC_ARM_REG_R0,value);u.reg_write(UC_ARM_REG_PC,u.reg_read(UC_ARM_REG_LR))
    def begin(self):
        result=self.call('collect_command',CTX)
        if self.spawned:
            assert self.native==self.symbols['collect_wait']
            assert self.descriptor==[0x5c,self.symbols['collect_init'],self.symbols['collect_destroy'],self.symbols['collect_update'],0x022037e9]
            assert self.call('collect_init',EFFECT,WORK)==1
        return result
    def tick(self):
        self.call('collect_update',EFFECT,WORK)
        if self.destroyed:self.call('collect_destroy',EFFECT,WORK)


def test_compiled_effect_shrinks_only_target_shows_ball_and_waits_for_cleanup():
    c=CPU();assert c.begin()==1
    for frame in range(36):
        assert c.call('collect_wait',CTX)==0
        c.tick()
        assert bool(c.hidden)==(frame>=11)
        assert c.destroyed==(frame==35)
    assert c.call('collect_wait',CTX)==1 and c.r(VAR)&0xffff==1
    assert c.hidden==[(OBJ,1)] and c.active==[1,0]
    assert len(c.scales)==13 and c.scales[-1]==(4096,4096,4096)
    assert [s[0] for s in c.scales[:-1]]==[4096-341*f for f in range(1,13)]
    assert not ({0x02069d68,0x021f771c}&set(c.calls)) # follower getters forbidden


@pytest.mark.parametrize('attribute,value',[('id',253),('id',255),('present',False),('sprite_id',1),('sprite_id',7000),('sprite',False),('resource',False),('resource_data',False),('spawn',False)])
def test_invalid_target_or_missing_resources_returns_failure_without_hiding(attribute,value):
    c=CPU();setattr(c,attribute,value)
    assert c.begin()==0 and c.r(VAR)&0xffff==0 and not c.hidden and not c.scales and c.native is None


@pytest.mark.parametrize('failure',['valid','sprite'])
def test_disappearing_actor_completes_failure_without_hanging_or_touching_follower(failure):
    c=CPU();assert c.begin()==1;setattr(c,failure,False);c.tick()
    assert c.destroyed and c.call('collect_wait',CTX)==1 and c.r(VAR)&0xffff==0
    assert not c.hidden and not c.scales


def test_runtime_append_keeps_every_original_command_and_overlay_byte():
    from sovereign_editor import character_runtime as cr,scene_runtime as sr
    baseline=Path(__file__).resolve().parents[1]/'projects/scyther-repair-1/baseline.nds'
    if not baseline.exists():pytest.skip('Pinned baseline is local and untracked')
    blob=baseline.read_bytes();info=cr.overlay(blob,131);resident=cr.overlay(blob,129)
    plan=sr.bindings(blob,{'files':{},'patches':[],'appends':{},'characters':[]})
    data=plan['files'][info['file_id']];assert data.startswith(info['data'])
    # GAMEPLAY-LOSS-001: the stock command table and count are never relocated.
    assert not any(p['kind'] in ('scene.command-table','scene.command-count') for p in plan['patches'])
    assert plan['files'][resident['file_id']].startswith(resident['data'])
    assert len(data)<0x10000 and set(plan['files'])=={info['file_id'],resident['file_id']}
    # Refuse a different baseline before changing a supplied plan.
    from sovereign_editor.formats import EditorError
    with pytest.raises(EditorError):sr.bindings(blob[:-1]+bytes([blob[-1]^1]),{'files':{},'patches':[]})
