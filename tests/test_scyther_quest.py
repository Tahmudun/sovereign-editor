"""Independent bytecode execution for quest progress, object lifetime and rewards.

This is a script interpreter, not an emulator or a native acceptance result.
"""
import copy
import json
import struct
import pytest
from sovereign_editor import event_sequences,scene_commands
from tools.tiana_encounter import operations as tiana_operations
from tools.scyther_quest import operations,STATE,MEDICINE


class QuestVM:
    def __init__(self):
        self.defs={k:{} for k in ('sequence','state','trainer')}
        for op in tiana_operations(json.load(open('tests/fixtures/tiana.character.json'))):
            r=op['request']
            if r['kind'] in self.defs:self.defs[r['kind']][r['key']]=copy.deepcopy(r['value'])
        for op in operations({'sequences':self.defs['sequence']}):
            r=op['request']
            if r.get('action')=='delete':
                self.defs[r['kind']].pop(r['key']);continue
            v=copy.deepcopy(r['value'])
            if r['kind']=='sequence':v['header']=op['context']['header']
            self.defs[r['kind']][r['key']]=v
        for i,s in enumerate(self.defs['state'].values()):s['variable']=0x4160+i
        for i,t in enumerate(self.defs['trainer'].values()):t['trainer_id']=738+i
        for i,s in enumerate(self.defs['sequence'].values()):s.update(npc_id=10+i,hide_flag=0xb47+i)
        self.values={};self.flags=set();self.positions={};self.alive=set();self.bag=0;self.party=1
        self.bag_full=False;self.gifts=0;self.medicine_grants=0;self.locked=False;self.header=None
        self.collection_failure=False;self.facings={};self.presentations=[];self.current_event=None;self.last_actor=None
        self.log=[];self.follower_free=False;self.follower_mode=48

    def get(self,key):return self.values.get(self.defs['state'][key]['variable'],0)
    def set(self,key,value):self.values[self.defs['state'][key]['variable']]=value
    def actors(self):return {k:s for k,s in self.defs['sequence'].items() if s['header']==self.header and s['kind']=='npc'}

    def load(self,header):
        self.header=header;self.positions={};self.alive=set()
        c=scene_commands.Code();scene_commands.emit_visibility(c,self.actors(),self.defs['state']);c.emit('H',2)
        self.execute(c.finish())
        for s in self.actors().values():
            if s['hide_flag'] not in self.flags:
                self.alive.add(s['npc_id']);self.positions[s['npc_id']]=(s['x'],s['z']);self.facings[s['npc_id']]=s['facing']

    def event(self,key,choices=()):
        s=self.defs['sequence'][key];self.current_event=key
        if s['header']!=self.header:self.load(s['header'])
        if s['kind']=='npc':assert s['npc_id'] in self.alive
        gather=next((n for n in s['nodes'] if n['op']=='gather'),None)
        # Geometry and all real gather approach routes are tested through Project.
        start=tuple(gather['destination']) if gather else (s['x']+1,s['z'])
        self.positions[255]=start;self.facings[255]=0
        self.last_actor=s['npc_id'] if s['kind']=='npc' else None
        routes={gather['id']:{start:[start]}} if gather else {}
        code=event_sequences.compile_sequence(s,self.defs['state'],self.defs['trainer'],1,
            {'actors':self.actors(),'variables':self.defs['state'],'routes':routes})
        self.execute(code,choices)

    def execute(self,code,choices=()):
        pc=0;comparison=0;choices=iter(choices);movement_pending=False
        def read(fmt):
            nonlocal pc
            result=struct.unpack_from('<'+fmt,code,pc);pc+=struct.calcsize('<'+fmt);return result
        for _ in range(2000):
            at=pc;op,=read('H');self.log.append(op)
            if op==2:assert not self.locked and not movement_pending and not self.follower_free and self.follower_mode==48;return
            if op in (17,18):
                var,value=read('2H')
                if op==18:value=self.values.get(value,0)
                actual=self.values.get(var,0);comparison=(actual>value)-(actual<value)
            elif op==28:
                condition,delta=read('Bi');assert condition in (0,1,2,5)
                if {0:comparison<0,1:comparison==0,2:comparison>0,5:comparison!=0}[condition]:pc+=delta
            elif op==22:delta,=read('i');pc+=delta
            elif op==41:var,value=read('2H');self.values[var]=value
            elif op in (30,31,32):
                flag,=read('H')
                if op==30:self.flags.add(flag)
                elif op==31:self.flags.discard(flag)
                else:comparison=0 if flag in self.flags else -1
            elif op==96:assert not self.locked;self.locked=True
            elif op==97:assert self.locked;self.locked=False
            elif op==73:assert self.locked;read('H')
            elif op==104:
                assert self.locked
                if self.last_actor is not None:self.facings[self.last_actor]=self.facings[255]^1
            elif op in (49,53,282):assert self.locked
            elif op==45:
                assert self.locked;message,=read('B')
                self.presentations.append({'event':self.current_event,'kind':'message','id':message,'positions':dict(self.positions),'facings':dict(self.facings)})
            elif op==63:assert self.locked;self.values[read('H')[0]]=next(choices)
            elif op==3:frames,var=read('2H');assert self.locked;self.values[var]=0
            elif op in (100,101):
                actor,=read('H');assert self.locked
                self.presentations.append({'event':self.current_event,'kind':'show' if op==100 else 'hide','actor':actor,'positions':dict(self.positions),'facings':dict(self.facings)})
                if op==101:assert actor in self.alive;self.alive.remove(actor);self.positions.pop(actor)
                else:
                    assert actor not in self.alive
                    spec=next(s for s in self.actors().values() if s['npc_id']==actor)
                    point=(spec['x'],spec['z'])
                    assert point not in self.positions.values()
                    self.alive.add(actor);self.positions[actor]=point;self.facings[actor]=spec['facing']
            elif op in (105,106):
                actor=255 if op==105 else read('H')[0];x,z=read('2H')
                self.values[x],self.values[z]=self.positions[actor]
            elif op==94:
                actor,delta=read('Hi');assert self.locked and not movement_pending and actor in self.positions
                cursor=pc+delta;movement_pending=True;x,z=self.positions[actor]
                actions=[];q=cursor
                while struct.unpack_from('<H',code,q)[0]!=254:actions.append(struct.unpack_from('<H',code,q)[0]);q+=4
                # Only player travel runs with the follower following (native 602 0 / 604 55).
                assert (self.follower_free and self.follower_mode==55)==(actor==255 and any(12<=a<=23 for a in actions))
                for __ in range(300):
                    action,count=struct.unpack_from('<2H',code,cursor);cursor+=4
                    if action==254:break
                    if 12<=action<=19:
                        self.facings[actor]=(action-12)%4
                        dx,dz=((0,-1),(0,1),(-1,0),(1,0))[(action-12)%4]
                        for ___ in range(count):
                            x+=dx;z+=dz
                            assert (x,z) not in [p for a,p in self.positions.items() if a!=actor]
                    else:
                        assert action in (0,1,2,3,49,75,103)
                        if action<4:self.facings[actor]=action
                        elif action==49:self.facings[actor]=1
                else:raise AssertionError('Missing movement terminator')
                self.positions[actor]=(x,z)
            elif op==95:assert movement_pending;movement_pending=False
            elif op==602:mode,=read('H');assert self.locked and mode in (0,1);self.follower_free=mode==0
            elif op==603:assert self.locked
            elif op==604:self.follower_mode,=read('H');assert self.follower_mode in (48,55) and self.follower_free==(self.follower_mode==55)
            elif op in (125,126,128):
                item,count,var=read('3H');assert self.locked and item==17
                success=(not self.bag_full) if op==125 else self.bag>=count
                if success and op==125:self.bag+=count;self.medicine_grants+=1
                if success and op==126:self.bag-=count
                self.values[var]=int(success)
            elif op==853:
                actor,=read('H');assert self.locked and actor in self.alive and actor<253
                self.presentations.append({'event':self.current_event,'kind':'collect','actor':actor,'positions':dict(self.positions),'facings':dict(self.facings)})
                self.values[scene_commands.TEMP_RESULT]=int(not self.collection_failure)
            elif op==137:
                species,level,item,form,ability,var=read('6H');assert (species,level,item,form,ability)==(123,8,0,0,0)
                success=self.party<6
                if success:self.party+=1;self.gifts+=1
                self.values[var]=int(success)
            else:raise AssertionError(f'Unknown command {op} at {at}')
        raise AssertionError('Script did not terminate')


def reach_corner(vm):
    for key,value in [('house_entry',1),('beach_find',2),('beach_flee',3),('town_flee',4),('road_flee',5),('corner_find',6)]:
        vm.event(key);assert vm.get(STATE)==value
        vm.load(vm.header)  # Leave/return or a normal save reload between beats.
        vm.event(key);assert vm.get(STATE)==value and not vm.locked


@pytest.mark.parametrize('party_full',[False,True])
@pytest.mark.parametrize('decline',[False,True])
@pytest.mark.parametrize('bag_full',[False,True])
def test_quest_recovery_and_exactly_once_rewards(party_full,decline,bag_full):
    vm=QuestVM();vm.bag_full=bag_full;reach_corner(vm)
    if bag_full:
        assert vm.bag==0 and vm.get(MEDICINE)==0
        vm.event('tiana_corner');assert vm.get(STATE)==6
        vm.bag_full=False;vm.event('tiana_corner')
    assert vm.medicine_grants==1 and vm.bag==1
    vm.event('tiana_corner');assert vm.medicine_grants==1
    if party_full:vm.party=6
    vm.event('scyther_corner',[0,1 if decline else 0])
    if decline or party_full:
        assert vm.get(STATE)==7 and vm.bag==0 and vm.gifts==0
        vm.load(33);vm.event('tiana_corner');assert vm.medicine_grants==1
        vm.party=1;vm.event('scyther_corner',[0])
    assert vm.get(STATE)==8 and vm.gifts==1 and vm.bag==0
    vm.load(33);assert not vm.alive
    vm.load(71);assert {s['npc_id'] for s in vm.actors().values() if s.get('presence')}==vm.alive
    assert not {'room_welcome','tiana_practice'} & vm.defs['sequence'].keys()
    assert not any(n['op'] in ('battle','partner') for s in vm.defs['sequence'].values() for n in s['nodes'])


def test_used_medicine_has_replacement_path_without_second_grant():
    vm=QuestVM();reach_corner(vm);vm.bag=0
    vm.event('scyther_corner',[0]);assert vm.get(STATE)==6 and vm.gifts==0
    vm.event('tiana_corner');assert vm.medicine_grants==1 and vm.bag==0
    vm.bag=1;vm.event('scyther_corner',[0,0]);assert vm.get(STATE)==8 and vm.gifts==1


def test_preexisting_potion_with_full_bag_does_not_grant_late_medicine():
    vm=QuestVM();vm.bag_full=True;vm.bag=1;reach_corner(vm)
    vm.event('scyther_corner',[0,1]);assert vm.get(STATE)==7 and vm.medicine_grants==0
    vm.bag_full=False;vm.event('tiana_corner');assert vm.medicine_grants==0


def test_played_room_welcome_state_does_not_skip_new_introduction():
    vm=QuestVM();vm.set('room_welcome',1);vm.set('tiana_progress',2)
    vm.event('house_entry');assert vm.get(STATE)==1 and vm.get('tiana_progress')==2


def test_happy_reaction_uses_stationary_hops():
    vm=QuestVM();vm.set(STATE,7);vm.load(33)
    s=vm.defs['sequence']['scyther_corner'];s['nodes']=[{'id':'happy','op':'reaction','actor':'scyther_corner','reaction':'happy','next':'end'},{'id':'end','op':'end'}]
    position=vm.positions[s['npc_id']];vm.event('scyther_corner');assert vm.positions[s['npc_id']]==position


@pytest.mark.parametrize('arrival',[(4,8),(8,7),(4,7)])
def test_authored_eastern_entry_arrival_and_loaded_room_fallback(arrival):
    vm=QuestVM();vm.load(71);vm.positions[255]=arrival
    spec=vm.defs['sequence']['house_entry']
    code=event_sequences.compile_sequence(spec,vm.defs['state'],vm.defs['trainer'],1,
        {'actors':vm.actors(),'variables':vm.defs['state'],
         'routes':{'gather':{(4,8):[(4,8),(5,8)]}}})
    vm.execute(code)
    assert vm.get(STATE)==1 and not vm.locked and not vm.alive
    assert vm.positions[255]==((5,8) if arrival==(4,8) else arrival)
    position=vm.positions[255];vm.execute(code)
    assert vm.get(STATE)==1 and vm.positions[255]==position and not vm.locked
