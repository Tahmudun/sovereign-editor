"""Format/binding checks, not Project integration or native acceptance."""
import base64
import copy
import json
from pathlib import Path
import struct

import pytest

from sovereign_editor import character_runtime as cr
from sovereign_editor.formats import EditorError

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def baseline():
    return (ROOT / 'projects/map-workflow-1/baseline.nds').read_bytes()


@pytest.fixture
def package():
    return json.loads((ROOT / 'tests/fixtures/tiana.character.json').read_text())


def test_empty_library_is_exact_noop(baseline):
    assert cr.bindings(baseline, []) == {'files': {}, 'patches': [], 'appends': {}, 'characters': []}


def test_bindings_preserve_original_tables(baseline, package):
    plan = cr.bindings(baseline, [package])
    assert plan['characters'] == [{'name': 'Tiana', 'sprite': 7000, 'front_class': 129, 'back_group': 17}]
    for i in (129, 131):
        original = cr.overlay(baseline, i)
        new = plan['files'][original['file_id']]
        assert len(new) > len(original['data'])
        # These data tables remain exact even after new versions are appended.
        ranges = [(0x6d1b, 129), (0x72a4, 516)] if i == 129 else [(0x2260, 9900)]
        for at, size in ranges:
            assert new[at:at + size] == original['data'][at:at + size]
    assert {k: len(v) for k, v in plan['appends'].items()} == {'a/0/8/1': 1, 'a/0/5/8': 5, 'a/0/0/6': 5}
    for p in plan['patches']:
        old = bytes.fromhex(p['before'])
        assert baseline[p['rom_offset']:p['rom_offset'] + len(old)] == old
        assert len(old) == len(bytes.fromhex(p['after']))


def test_reject_changed_baseline(baseline, package):
    changed = bytearray(baseline)
    changed[-1] ^= 1
    with pytest.raises(EditorError, match='pinned baseline'):
        cr.bindings(changed, [package])


def test_reject_extra_palette_colors(package):
    raw = bytearray(cr.decoded(package['front'][1]))
    raw[72] = 1
    package['front'][1] = base64.b64encode(raw).decode()
    with pytest.raises(EditorError, match='palette'):
        cr.validate_package(package)


def test_reject_wrong_back_cell_count(package):
    raw = bytearray(cr.decoded(package['back'][2]))
    struct.pack_into('<H', raw, 24, 1)
    package['back'][2] = base64.b64encode(raw).decode()
    with pytest.raises(EditorError, match='cell layout'):
        cr.validate_package(package)


def test_library_capacity_and_unique_bindings(baseline, package):
    packages = [copy.deepcopy(package) for _ in range(8)]
    plan = cr.bindings(baseline, packages)
    assert [v['sprite'] for v in plan['characters']] == list(range(7000, 7008))
    assert len(plan['files'][129]) <= 0x8000
    assert len(plan['files'][131]) <= 0x10000
    with pytest.raises(EditorError, match='eight'):
        cr.bindings(baseline, packages + [package])


def bl_target(patch):
    # Independent ARMv5 Thumb BL decoding from the exported instruction.
    a,b=struct.unpack('<2H',bytes.fromhex(patch['after']))
    offset=((a&2047)<<12)|((b&2047)<<1)
    if offset&(1<<22):offset-=1<<23
    # Qualified ARM9 ROM offset0x4000 maps to address0x02000000.
    return patch['rom_offset']-0x4000+0x02000000+4+offset


def execute_hook(payload,address,registers):
    """Small ARMv5 instruction interpreter, used only for the emitted hooks."""
    regs=list(registers);pc=address;carry=False
    for _ in range(40):
        word=struct.unpack_from('<H',payload,pc-0x023d8000)[0];next_pc=pc+2
        if word&0xf800==0x4800:
            at=((pc+4)&~3)+(word&255)*4
            regs[(word>>8)&7]=struct.unpack_from('<I',payload,at-0x023d8000)[0]
        elif word&0xf800==0x2800:carry=regs[(word>>8)&7]>=(word&255)
        elif word&0xffc0==0x4280:carry=regs[word&7]>=regs[(word>>3)&7]
        elif word&0xff00 in (0xd200,0xd300):
            take=carry if word&0xff00==0xd200 else not carry
            delta=word&255
            if delta&128:delta-=256
            if take:next_pc=pc+4+2*delta
        elif word&0xf800==0x2000:regs[(word>>8)&7]=word&255
        elif word&0xf800==0x3000:regs[(word>>8)&7]+=word&255
        elif word&0xf800==0x3800:regs[(word>>8)&7]-=word&255
        elif word&0xf800==0:regs[word&7]=(regs[(word>>3)&7]<<((word>>6)&31))&0xffffffff
        elif word&0xffc0==0x4300:regs[word&7]|=regs[(word>>3)&7]
        elif word&0xfe00==0x1c00:regs[word&7]=regs[(word>>3)&7]+((word>>6)&7)
        elif word&0xff87==0x4700:return regs[(word>>3)&15],regs
        elif word==0x46c0:pass
        else:raise AssertionError(f'Unexpected instruction {word:04x}')
        pc=next_pc
    raise AssertionError('Hook did not return')


def test_back_hook_all_stock_and_new_classes(baseline,package):
    plan=cr.bindings(baseline,[package]*8)
    patch=next(p for p in plan['patches'] if p['kind']=='character.partner-back')
    target=bl_target(patch)
    for trainer_class in range(201):
        for link in (0,1):
            r=list(range(16));r[0]=trainer_class;r[1]=link;r[14]=0x02000101
            destination,after=execute_hook(plan['files'][129],target,r)
            if 129<=trainer_class<137:
                assert destination==r[14] and after[0]==trainer_class-129+17
            else:assert destination==0x0207280d and after[0]==trainer_class
            assert after[1]==link and after[4:]==r[4:]


def test_practice_hook_is_guarded_and_preserves_setup(baseline,package):
    plan=cr.bindings(baseline,[package],trainer_count=3)
    patch=next(p for p in plan['patches'] if p['kind']=='trainer.practice-return')
    for trainer in (0,1,737,738,739,740,741,65535):
        for battle_type in (1,3,0x13,0x4b,0x801):
            r=list(range(16));r[4]=battle_type;r[7]=trainer;r[14]=0x02000101
            destination,after=execute_hook(plan['files'][129],bl_target(patch),r)
            expected=battle_type|0x800 if 738<=trainer<741 else battle_type
            assert destination==r[14] and after[0]==11 and after[1]==after[4]==expected
            assert after[2:4]==r[2:4] and after[5:]==r[5:]
