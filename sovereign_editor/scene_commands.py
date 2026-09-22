"""Bounded native scene instructions shared by the sequence compiler and editor.

Opcode layouts: pinned pret/pokeheartgold e97c7fc, evidence/scyther-quest-1.
This module emits bytes only. Project owns state and file writes.
"""
import struct
from .formats import require

PLAYER=0xff
TEMP_X,TEMP_Z,TEMP_RESULT=0x8008,0x8009,0x800a
OPS={'move','gather','face','reaction','wait','sound','give_item','take_item','has_item','give_mon','sync'}
# Happy: two fast hops in place facing south, not a displacement or an alert.
# Pinned movement.inc JumpOnSpotFastSouth (49). Native visual check is separate.
REACTIONS={'exclamation':75,'question':103,'happy':49}
SOUNDS={'heal':1516,'confirm':1521,'receive':1801}

def integer(v,lo,hi):return type(v) is int and lo<=v<=hi

def validate(n):
    op=n['op'];fields={'id','op'};targets=['next']
    if op in ('move','face','reaction'):
        fields.add('actor');require(isinstance(n.get('actor'),str) and bool(n['actor']), 'Choose a scene actor','INVALID_EVENT')
    if op=='move':
        fields|={'path','speed'}
        path=n.get('path');require(isinstance(path,list) and 1<=len(path)<=65,'A route needs 1..65 waypoints','INVALID_EVENT')
        require(all(isinstance(p,list) and len(p)==2 and all(integer(v,0,65535) for v in p) for p in path),'Invalid route coordinates','INVALID_EVENT')
        require(all((a[0]==b[0]) != (a[1]==b[1]) for a,b in zip(path,path[1:])),'Route segments must be horizontal or vertical','INVALID_EVENT')
        require(sum(abs(a[0]-b[0])+abs(a[1]-b[1]) for a,b in zip(path,path[1:]))<=128,'Route exceeds 128 tiles','RESOURCE_CAPACITY')
        require(n.get('speed') in ('walk','run'),'Choose walking or running','INVALID_EVENT')
    elif op=='gather':
        fields.add('destination')
        require(isinstance(n.get('destination'),list) and len(n['destination'])==2 and all(integer(v,0,65535) for v in n['destination']),'Choose a player staging tile','INVALID_EVENT')
    elif op=='face':
        fields.add('direction');require(integer(n.get('direction'),0,3),'Choose a direction','INVALID_EVENT')
    elif op=='reaction':
        fields.add('reaction');require(n.get('reaction') in REACTIONS,'Choose a supported reaction','INVALID_EVENT')
    elif op=='wait':
        fields.add('frames');require(integer(n.get('frames'),1,600),'Pause needs 1..600 frames','INVALID_EVENT')
    elif op=='sound':
        fields.add('sound');require(n.get('sound') in SOUNDS,'Choose a supported sound','INVALID_EVENT')
    elif op in ('give_item','take_item','has_item'):
        fields|={'item','count'};targets=['yes','no']
        require(integer(n.get('item'),1,536) and integer(n.get('count'),1,99),'Use a base item1..536 and count1..99','INVALID_EVENT')
    elif op=='give_mon':
        fields|={'species','level'};targets=['yes','no']
        require(integer(n.get('species'),1,493) and integer(n.get('level'),1,100),'Use a base species and level1..100','INVALID_EVENT')
    elif op=='sync':pass
    else:require(False,'Unknown scene operation','INVALID_EVENT')
    return fields,targets


class Code:
    def __init__(self):self.data=bytearray();self.labels={};self.jumps=[];self.movements=[]
    def emit(self,fmt,*values):self.data.extend(struct.pack('<'+fmt,*values))
    def label(self,key):
        require(key not in self.labels,'Duplicate compiler label');self.labels[key]=len(self.data)
    def jump(self,label,condition=None):
        self.emit('H' if condition is None else 'HB',*([22] if condition is None else [28,condition]))
        self.jumps.append((len(self.data),label));self.emit('i',0)
    def compare(self,var,value):self.emit('3H',17,var,value)
    def movement(self,actor,commands):
        self.emit('2H',94,actor);at=len(self.data);self.emit('i',0)
        self.movements.append((at,list(commands)+[(254,0)]));self.emit('H',95)
    def finish(self):
        for at,commands in self.movements:
            self.data.extend(b'\0'*(-len(self.data)%4));start=len(self.data)
            struct.pack_into('<i',self.data,at,start-at-4)
            for cmd,count in commands:self.emit('2H',cmd,count)
        for at,label in self.jumps:struct.pack_into('<i',self.data,at,self.labels[label]-at-4)
        require(len(self.data)<=32768,'Scene script exceeds32KiB','RESOURCE_CAPACITY')
        return bytes(self.data)


def emit_node(c,n,env):
    op=n['op'];actors=env.get('actors',{})
    if op in ('move','face','reaction'):
        require(n['actor']=='player' or n['actor'] in actors,'Unknown actor in this area','INVALID_EVENT')
        actor=PLAYER if n['actor']=='player' else actors[n['actor']]['npc_id']
        if op=='move':
            # Never march from an unexpected position. Re-entry/save recovery is
            # stage-owned; an invalid starting point returns control safely.
            c.emit('3H',105,TEMP_X,TEMP_Z) if actor==PLAYER else c.emit('4H',106,actor,TEMP_X,TEMP_Z)
            for var,value in zip((TEMP_X,TEMP_Z),n['path'][0]):c.compare(var,value);c.jump('$exit',5)
            commands=[]
            for (x,z),(xx,zz) in zip(n['path'],n['path'][1:]):
                direction=(3 if xx>x else 2) if x!=xx else (1 if zz>z else 0)
                commands.append(((12 if n['speed']=='walk' else 16)+direction,abs(xx-x)+abs(zz-z)))
            if commands:c.movement(actor,commands)
        elif op=='face':c.movement(actor,[(n['direction'],1)])
        else:c.movement(actor,[(REACTIONS[n['reaction']],2 if n['reaction']=='happy' else 1)])
    elif op=='gather':
        routes=env.get('routes',{}).get(n['id'])
        require(routes is not None,'Player approach needs a conditional trigger','INVALID_EVENT')
        c.emit('3H',105,TEMP_X,TEMP_Z)
        for i,(start,path) in enumerate(routes.items()):
            tag='$gather'+n['id']+str(i)
            c.compare(TEMP_X,start[0]);c.jump(tag,5)
            c.compare(TEMP_Z,start[1]);c.jump(tag,5)
            if len(path)>1:
                commands=[]
                for (x,z),(xx,zz) in zip(path,path[1:]):
                    direction=(3 if xx>x else 2) if x!=xx else (1 if zz>z else 0)
                    if commands and commands[-1][0]==12+direction:commands[-1]=(commands[-1][0],commands[-1][1]+1)
                    else:commands.append((12+direction,1))
                c.movement(PLAYER,commands)
            c.jump(n['next']);c.label(tag)
        c.jump('$exit');return
    elif op=='wait':c.emit('3H',3,n['frames'],TEMP_RESULT)
    elif op=='sound':c.emit('2H',73,SOUNDS[n['sound']])
    elif op in ('give_item','take_item','has_item'):
        c.emit('4H',{'give_item':125,'take_item':126,'has_item':128}[op],n['item'],n['count'],TEMP_RESULT)
        c.compare(TEMP_RESULT,1);c.jump(n['yes'],1);c.jump(n['no']);return
    elif op=='give_mon':
        c.emit('7H',137,n['species'],n['level'],0,0,0,TEMP_RESULT)
        c.compare(TEMP_RESULT,1);c.jump(n['yes'],1);c.jump(n['no']);return
    elif op=='sync':emit_visibility(c,actors,env['variables'],live=True,prefix='$sync'+n['id'])
    c.jump(n['next'])


def emit_visibility(c,actors,variables,live=False,prefix='$presence'):
    if live:
        # Remove obsolete objects before creating their replacements. Staged
        # actors can deliberately share a tile in mutually exclusive stages.
        for key,s in actors.items():
            if not s.get('presence'):continue
            tag=prefix+key+'hide_end';v=s['presence'];flag=s['hide_flag']
            for value in v['values']:
                c.compare(variables[v['state']]['variable'],value);c.jump(tag,1)
            c.emit('2H',32,flag);c.jump(tag,1)
            c.emit('4H',30,flag,101,s['npc_id']);c.label(tag)
    for key,s in actors.items():
        if not s.get('presence'):continue
        tag=prefix+key;v=s['presence'];flag=s['hide_flag']
        for value in v['values']:
            c.compare(variables[v['state']]['variable'],value);c.jump(tag+'show',1)
        if not live:c.emit('2H',30,flag)
        c.jump(tag+'end');c.label(tag+'show')
        if live:
            c.emit('2H',32,flag);c.jump(tag+'end',0)
        c.emit('2H',31,flag)
        # Presence actors are stationary by qualification. Do not reacquire an
        # NPC-talk lock here: an earlier hide may delete lastInteracted.
        if live:c.emit('2H',100,s['npc_id'])
        c.label(tag+'end')
