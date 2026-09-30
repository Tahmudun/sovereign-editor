"""Bounded native scene instructions shared by the sequence compiler and editor.

Opcode layouts: pinned pret/pokeheartgold e97c7fc, evidence/scyther-quest-1.
This module emits bytes only. Project owns state and file writes.
"""
import struct
from .formats import require

PLAYER=0xff
TEMP_X,TEMP_Z,TEMP_RESULT=0x8008,0x8009,0x800a
# Native follower wrapper for scripted player travel (pret scr_seq_0850_T21,
# scr_seq_0225_R29): unpause, wait, follow-the-player movement 55; afterwards
# wait, pause, restore movement 48. All three commands are no-ops without a follower.
TOGGLE_FOLLOWER,WAIT_FOLLOWER,FOLLOWER_MOVEMENT=602,603,604
FOLLOW_SCRIPT,FOLLOW_NORMAL=55,48
OPS={'move','gather','face','look','pose','reaction','wait','sound','give_item','take_item','has_item','give_mon','collect','sync','found_item'}
MOVEMENT_OPS={'move','gather','face','look','reaction'}   # may emit movement data
# Happy: two fast hops in place facing south, not a displacement or an alert.
# Pinned movement.inc JumpOnSpotFastSouth (49). Native visual check is separate.
REACTIONS={'exclamation':75,'question':103,'happy':49}
SOUNDS={'heal':1516,'confirm':1521,'receive':1801}
# Stock item-ball presentation (pret scr_seq_0141, bank 199 via msgbox_extern): pocket
# fanfare (std 2001), "[player] found / a [item]!" (3), TM "[item] [move]!" or
# quantity "[count] [items]!" (6), then "[player] put the [item] / in the [pocket] Pocket." (9).
ITEM_TEXT,FOUND,FOUND_PAIR,PUT_AWAY=199,3,6,9
ITEM_VAR,COUNT_VAR=0x8004,0x8005

def integer(v,lo,hi):return type(v) is int and lo<=v<=hi

def direction_toward(origin,target,axis):
    """Cardinal gaze with an authored priority for diagonal subjects."""
    dx,dz=target[0]-origin[0],target[1]-origin[1]
    require(dx or dz,'An actor cannot look toward its own tile','INVALID_EVENT')
    return (3 if dx>0 else 2) if dx and (axis=='x' or not dz) else (1 if dz>0 else 0)

def validate(n):
    op=n['op'];fields={'id','op'};targets=['next']
    if op in ('move','face','look','reaction','collect'):
        fields.add('actor');require(isinstance(n.get('actor'),str) and bool(n['actor']), 'Choose a scene actor','INVALID_EVENT')
    if op=='move':
        fields|={'path','speed'}
        path=n.get('path');require(isinstance(path,list) and 1<=len(path)<=65,'A route needs 1..65 waypoints','INVALID_EVENT')
        require(all(isinstance(p,list) and len(p)==2 and all(integer(v,0,65535) for v in p) for p in path),'Invalid route coordinates','INVALID_EVENT')
        require(all((a[0]==b[0]) != (a[1]==b[1]) for a,b in zip(path,path[1:])),'Route segments must be horizontal or vertical','INVALID_EVENT')
        require(sum(abs(a[0]-b[0])+abs(a[1]-b[1]) for a,b in zip(path,path[1:]))<=128,'Route exceeds 128 tiles','RESOURCE_CAPACITY')
        require(n.get('speed') in ('walk','run'),'Choose walking or running','INVALID_EVENT')
    elif op=='gather':
        # Optional no: where to continue when the player is on none of the
        # precomputed approaches (e.g. a save loaded mid-room); default abort.
        fields.add('destination')
        if 'no' in n:targets=['next','no']
        require(isinstance(n.get('destination'),list) and len(n['destination'])==2 and all(integer(v,0,65535) for v in n['destination']),'Choose a player staging tile','INVALID_EVENT')
    elif op=='face':
        fields.add('direction');require(integer(n.get('direction'),0,3),'Choose a direction','INVALID_EVENT')
    elif op=='look':
        fields|={'target','axis'}
        require(isinstance(n.get('target'),str) and n['target'] and n['target']!=n['actor'], 'Choose a different actor to look toward','INVALID_EVENT')
        require(n.get('axis') in ('x','z'),'Choose X or Z priority for a diagonal gaze','INVALID_EVENT')
    elif op=='pose':
        fields.add('actors');poses=n.get('actors')
        require(isinstance(poses,dict) and bool(poses),'A pose checkpoint needs actors','INVALID_EVENT')
        for key,p in poses.items():
            require(isinstance(key,str) and isinstance(p,dict) and bool(p) and set(p)<={'tile','facing','visible'},'Invalid pose assertion','INVALID_EVENT')
            if 'tile' in p:require(isinstance(p['tile'],list) and len(p['tile'])==2 and all(integer(v,0,65535) for v in p['tile']),'Invalid pose tile','INVALID_EVENT')
            if 'facing' in p:require(integer(p['facing'],0,3),'Invalid pose facing','INVALID_EVENT')
            if 'visible' in p:require(type(p['visible']) is bool,'Invalid pose visibility','INVALID_EVENT')
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
        # Optional form (assets-gameplay v1): qualified species/forms are checked against
        # the ROM roster by story_authoring; absent form keeps the historical bytes.
        fields|={'species','level','form'};targets=['yes','no']
        require(integer(n.get('species'),1,1075) and integer(n.get('level'),1,100) and integer(n.get('form',0),0,31),
                'Use a qualified species, form 0..31 and level1..100','INVALID_EVENT')
    elif op=='found_item':
        fields|={'item','count'}
        require(integer(n.get('item'),1,536) and integer(n.get('count'),1,99),'Use a base item1..536 and count1..99','INVALID_EVENT')
    elif op=='collect':
        targets=['yes','no']
        require(n['actor']!='player','Collection needs a Pokémon actor','INVALID_EVENT')
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
        # Player travel keeps the follower with the player instead of leaving it
        # paused where LockAll stopped it. Turns and reactions leave it paused.
        travel=actor==PLAYER and any(12<=cmd<=23 for cmd,_ in commands)
        if travel:self.emit('5H',TOGGLE_FOLLOWER,0,WAIT_FOLLOWER,FOLLOWER_MOVEMENT,FOLLOW_SCRIPT)
        self.emit('2H',94,actor);at=len(self.data);self.emit('i',0)
        self.movements.append((at,list(commands)+[(254,0)]));self.emit('H',95)
        if travel:self.emit('5H',WAIT_FOLLOWER,TOGGLE_FOLLOWER,1,FOLLOWER_MOVEMENT,FOLLOW_NORMAL)
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
    if op in ('move','face','look','reaction','collect'):
        require(n['actor']=='player' or n['actor'] in actors,'Unknown actor in this area','INVALID_EVENT')
        actor=PLAYER if n['actor']=='player' else actors[n['actor']]['npc_id']
        if op=='collect':
            from .scene_runtime import COLLECT_OPCODE
            require(actor<253 and 364<=actors[n['actor']].get('stock_sprite',0)<=993,'Collection needs a supported Pokémon sprite','INVALID_EVENT')
            c.emit('2H',COLLECT_OPCODE,actor)
            c.compare(TEMP_RESULT,1);c.jump(n['yes'],1);c.jump(n['no']);return
        if op=='move':
            # Never march from an unexpected position. Re-entry/save recovery is
            # stage-owned; an invalid starting point returns control safely.
            c.emit('3H',105,TEMP_X,TEMP_Z) if actor==PLAYER else c.emit('4H',106,actor,TEMP_X,TEMP_Z)
            for var,value in zip((TEMP_X,TEMP_Z),n['path'][0]):c.compare(var,value);c.jump('$abort',5)
            commands=[]
            for (x,z),(xx,zz) in zip(n['path'],n['path'][1:]):
                direction=(3 if xx>x else 2) if x!=xx else (1 if zz>z else 0)
                commands.append(((12 if n['speed']=='walk' else 16)+direction,abs(xx-x)+abs(zz-z)))
            if commands:c.movement(actor,commands)
        elif op=='face':c.movement(actor,[(n['direction'],1)])
        elif op=='look':
            target=n['target'];require(target=='player' or target in actors,'Unknown gaze target','INVALID_EVENT')
            target_id=PLAYER if target=='player' else actors[target]['npc_id']
            # Read both live positions. Compare native variables (opcode 18),
            # never infer facing from the previous walking direction.
            c.emit('3H',105,TEMP_X,TEMP_Z) if actor==PLAYER else c.emit('4H',106,actor,TEMP_X,TEMP_Z)
            tx,tz=0x800b,0x800d
            c.emit('3H',105,tx,tz) if target_id==PLAYER else c.emit('4H',106,target_id,tx,tz)
            base='$look'+n['id'];axes=[(TEMP_X,tx,2,3),(TEMP_Z,tz,0,1)]
            if n['axis']=='z':axes.reverse()
            for a,b,low,high in axes:
                c.emit('3H',18,a,b);c.jump(base+str(low),2);c.jump(base+str(high),0)
            c.jump('$abort')
            for d in range(4):
                c.label(base+str(d));c.movement(actor,[(d,1)]);c.jump(n['next'])
            return
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
        c.jump(n.get('no','$abort'));return
    elif op=='wait':c.emit('3H',3,n['frames'],TEMP_RESULT)
    elif op=='sound':c.emit('2H',73,SOUNDS[n['sound']])
    elif op in ('give_item','take_item','has_item'):
        c.emit('4H',{'give_item':125,'take_item':126,'has_item':128}[op],n['item'],n['count'],TEMP_RESULT)
        c.compare(TEMP_RESULT,1);c.jump(n['yes'],1);c.jump(n['no']);return
    elif op=='found_item':
        # Announces an item that a give_item step already granted (R82-ITEM-01).
        tag='$found'+n['id']
        c.emit('3H',41,ITEM_VAR,n['item']);c.emit('3H',41,COUNT_VAR,n['count'])
        c.emit('2H',20,2001);c.emit('HB',190,0)
        if n['count']==1:
            c.emit('3H',129,ITEM_VAR,TEMP_RESULT);c.compare(TEMP_RESULT,1);c.jump(tag+'tm',1)
            c.emit('HBH',843,1,ITEM_VAR);c.emit('3H',440,ITEM_TEXT,FOUND);c.jump(tag+'put')
            c.label(tag+'tm');c.emit('HBH',843,1,ITEM_VAR);c.emit('HBH',196,2,ITEM_VAR)
        else:c.emit('HBH',198,1,COUNT_VAR);c.emit('HBH',844,2,ITEM_VAR)
        c.emit('3H',440,ITEM_TEXT,FOUND_PAIR)
        c.label(tag+'put');c.emit('H',79);c.emit('HB',190,0)
        c.emit('HBH',194 if n['count']==1 else 844,1,ITEM_VAR)
        c.emit('3H',130,ITEM_VAR,TEMP_RESULT);c.emit('HBH',195,2,TEMP_RESULT)
        c.emit('3H',440,ITEM_TEXT,PUT_AWAY);c.emit('2H',50,53)
    elif op=='give_mon':
        c.emit('7H',137,n['species'],n['level'],0,n.get('form',0),0,TEMP_RESULT)
        c.compare(TEMP_RESULT,1);c.jump(n['yes'],1);c.jump(n['no']);return
    elif op=='sync':emit_visibility(c,actors,env['variables'],live=True,prefix='$sync'+n['id'])
    elif op=='pose':pass  # authoring assertion; no runtime work
    c.jump(n['next'])


def state_jump(c,state,value,label):
    """Jump to label when a named state equals value: a variable compare, or
    checkflag for an on/off state (1 = set, 0 = clear)."""
    if state.get('switch'):c.emit('2H',32,state['flag']);c.jump(label,1 if value else 5)
    else:c.compare(state['variable'],value);c.jump(label,1)

def state_jump_unless(c,state,value,label):
    if state.get('switch'):c.emit('2H',32,state['flag']);c.jump(label,5 if value else 1)
    else:c.compare(state['variable'],value);c.jump(label,5)

def state_set(c,state,value):
    if state.get('switch'):c.emit('2H',30 if value else 31,state['flag'])
    else:c.emit('3H',41,state['variable'],value)

def emit_visibility(c,actors,variables,live=False,prefix='$presence',show=True):
    """Stage visibility. show=False only hides: a safe abort cannot know the player's tile."""
    if live:
        # Remove obsolete objects before creating their replacements. Staged
        # actors can deliberately share a tile in mutually exclusive stages.
        for key,s in actors.items():
            if not s.get('presence'):continue
            tag=prefix+key+'hide_end';v=s['presence'];flag=s['hide_flag']
            for value in v['values']:
                state_jump(c,variables[v['state']],value,tag)
            c.emit('2H',32,flag);c.jump(tag,1)
            c.emit('4H',30,flag,101,s['npc_id']);c.label(tag)
    if not show:return
    for key,s in actors.items():
        if not s.get('presence'):continue
        tag=prefix+key;v=s['presence'];flag=s['hide_flag']
        for value in v['values']:
            state_jump(c,variables[v['state']],value,tag+'show')
        if not live:c.emit('2H',30,flag)
        c.jump(tag+'end');c.label(tag+'show')
        if live:
            c.emit('2H',32,flag);c.jump(tag+'end',0)
        c.emit('2H',31,flag)
        # Presence actors are stationary by qualification. Do not reacquire an
        # NPC-talk lock here: an earlier hide may delete lastInteracted.
        if live:c.emit('2H',100,s['npc_id'])
        c.label(tag+'end')
