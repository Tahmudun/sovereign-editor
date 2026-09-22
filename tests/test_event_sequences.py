"""Independent interpreter exercises compiled dialogue branches and lock exits."""
import copy
import json
import struct
import pytest
from sovereign_editor import event_sequences as seq
from sovereign_editor.formats import EditorError
from tools.tiana_encounter import operations


def fixture():
    ops=operations(json.load(open('tests/fixtures/tiana.character.json')))
    values={k:{} for k in ('state','trainer','sequence')}
    for op in ops:
        r=op['request'];k=r['kind']
        if k in values:values[k][r['key']]=copy.deepcopy(r['value'])
    for i,s in enumerate(values['state'].values()):s['variable']=0x4160+i
    for i,t in enumerate(values['trainer'].values()):t['trainer_id']=738+i
    return values


def run(code,values=None,choices=(),wins=()):
    values=dict(values or {});choices=iter(choices);wins=iter(wins);pc=0;locked=False;shown=[];battles=[];comp=0
    def read(fmt):
        nonlocal pc
        out=struct.unpack_from('<'+fmt,code,pc);pc+=struct.calcsize('<'+fmt);return out
    for _ in range(1000):
        op,=read('H')
        if op==2:
            assert not locked and pc<=len(code);return values,shown,battles
        if op==96:assert not locked;locked=True
        elif op==97:assert locked;locked=False
        elif op==73:assert read('H')==(1500,)
        elif op in (104,49,53,282):assert locked
        elif op==45:assert locked;shown.append(read('B')[0])
        elif op==63:values[read('H')[0]]=next(choices)
        elif op==17:
            variable,value=read('2H');comp=(values.get(variable,0)>value)-(values.get(variable,0)<value)
        elif op==41:
            variable,value=read('2H');values[variable]=value
        elif op==22:
            delta,=read('i');pc+=delta
        elif op==28:
            condition,delta=read('Bi');assert condition in (1,5)
            if (comp==0)==(condition==1):pc+=delta
        elif op==213:
            args=read('2H2B');assert args[1:]==(0,1,0);battles.append(args[0]);locked=False # Native encounter restoration unpauses objects.
        elif op==562:battles.append(read('3HB'));locked=False
        elif op==220:values[read('H')[0]]=next(wins)
        else:raise AssertionError(f'Unexpected opcode {op} at {pc-2}')
        assert 0<=pc<len(code)
    raise AssertionError('Script failed to terminate')


def test_all_choice_and_battle_outcomes_release_and_replay():
    f=fixture();s=f['sequence']['tiana_practice'];code=seq.compile_sequence(s,f['state'],f['trainer'],5)
    v,m,b=run(code,choices=[1]);assert not b and v.get(0x4161,0)==0
    for win in (0,1):
        v,m,b=run(code,choices=[0,1],wins=[win]);assert v[0x4161]==1 and b==[738]
        v,m,b=run(code,v,choices=[0],wins=[win]);assert v[0x4161]==2 and b==[(738,739,740,0)]
        v,m,b=run(code,v,choices=[1,1]);assert v[0x4161]==2 and not b
        v,m,b=run(code,v,choices=[1,0,0],wins=[win,win]);assert v[0x4161]==2 and len(b)==2


def test_missing_postbattle_relock_is_detected():
    f=fixture();code=seq.compile_sequence(f['sequence']['tiana_practice'],f['state'],f['trainer'],5)
    # Keep offsets stable: turn LockAll into another harmless HealParty. The
    # independent VM must reject the first unprotected post-battle operation.
    marker=struct.pack('<4H',96,220,seq.RESULT,282)
    assert code.count(marker)==2
    broken=code.replace(marker,struct.pack('<4H',282,220,seq.RESULT,282))
    with pytest.raises(AssertionError):run(broken,choices=[0,1],wins=[1])


def test_one_time_trigger_remembers_and_skips_dialogue():
    f=fixture();code=seq.compile_sequence(f['sequence']['room_welcome'],f['state'],f['trainer'],5)
    v,m,b=run(code);assert len(m)==2 and v[0x4160]==1 and not b
    v,m,b=run(code,v);assert not m and v[0x4160]==1


@pytest.mark.parametrize('change',['cycle','missing_target','missing_state','missing_trainer','unreachable','long_page'])
def test_invalid_graphs_refused(change):
    f=fixture();s=f['sequence']['tiana_practice'];n=s['nodes']
    if change=='cycle':n[1]['next']='progress'
    elif change=='missing_target':n[1]['next']='absent'
    elif change=='missing_state':n[0]['state']='absent'
    elif change=='missing_trainer':n[3]['trainer']='absent'
    elif change=='unreachable':n.append({'id':'unused','op':'end'})
    else:n[1]['pages']=['x'*29]
    with pytest.raises(EditorError):seq.validate(n,f['state'],f['trainer'])
