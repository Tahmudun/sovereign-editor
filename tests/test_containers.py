"""Independent archive decoding and bounded ROM relocation tests."""
import struct
import ndspy.narc
import ndspy.fnt
import pytest
from ndspy._common import crc16
from sovereign_editor.containers import replace_members,replace_file
from sovereign_editor.formats import EditorError


def archive():
    n=ndspy.narc.NARC();n.files=[b'first123',b'unknown bytes',b'last']
    return n.save()


def test_narc_grow_shrink_and_noop_preserve_unknown_data():
    raw=archive()
    assert replace_members(raw,{})[0]==raw
    assert replace_members(raw,{0:b'first123'})[0]==raw
    for payload in (b'long replacement'*5,b'X',b''):
        changed,report=replace_members(raw,{1:payload})
        n=ndspy.narc.NARC(changed)
        assert n.files==[b'first123',payload,b'last']
        assert b'unknown bytes' in changed # old data is retained, now unreferenced
        assert report[0]['relocated']
        assert changed[:8]==raw[:8]


def test_narc_malformed_and_overlapped_refuse():
    raw=bytearray(archive())
    with pytest.raises(EditorError):replace_members(bytes(raw[:-1]),{0:b'a'})
    struct.pack_into('<II',raw,36,*struct.unpack_from('<II',raw,28))
    with pytest.raises(EditorError,match='Overlapping'):replace_members(bytes(raw),{0:b'a'})


def rom():
    result=bytearray(b'\xff'*4096)
    result[0x12]=0;result[0x14]=0 # capacity 128KiB
    names=ndspy.fnt.save(ndspy.fnt.Folder(files=['map','other'],firstID=0))
    result[0x200:0x200+len(names)]=names
    struct.pack_into('<4I',result,0x40,0x200,len(names),0x300,16)
    struct.pack_into('<4I',result,0x300,0x400,0x408,0x500,0x50b)
    result[0x400:0x408]=b'old_data';result[0x500:0x50b]=b'keep me !!!'
    struct.pack_into('<I',result,0x80,len(result))
    struct.pack_into('<H',result,0x15e,crc16(result[:0x15e]))
    return bytes(result)


def test_rom_append_audits_only_metadata_and_capacity():
    before=rom()
    assert replace_file(before,'map',b'old_data')[0]==before
    changed,report=replace_file(before,'map',b'new file payload')
    assert report['start']%512==0 and report['start']==len(before)
    offset,end=struct.unpack_from('<II',changed,0x300)
    assert changed[offset:end]==b'new file payload'
    assert changed[0x500:0x50b]==b'keep me !!!'
    restored=bytearray(changed[:len(before)])
    for field in report['metadata_fields']:
        o,n=field['offset'],field['bytes'];restored[o:o+n]=before[o:o+n]
    assert bytes(restored)==before
    assert crc16(changed[:0x15e])==struct.unpack_from('<H',changed,0x15e)[0]
    with pytest.raises(EditorError) as exc:replace_file(before,'map',b'X'*131072)
    assert exc.value.code=='ROM_CAPACITY'
