"""Targeted flat-plane qualification must never silently approve a slope."""
import struct
import pytest
from sovereign_editor.formats import flat_height_plates,EditorError


def terrain():
    points=[(0,0),(2*65536,2*65536),(3*65536,0),(5*65536,2*65536)]
    normals=[(0,4096,0),(2048,4096,0)]
    data=b'BDHC'+struct.pack('<6H',4,2,1,2,0,0)
    data+=b''.join(struct.pack('<2i',*p) for p in points)
    data+=b''.join(struct.pack('<3i',*p) for p in normals)
    data+=struct.pack('<i',-65536)
    data+=struct.pack('<8H',0,1,0,0,2,3,1,0)
    return struct.pack('<4I',0,0,0,len(data))+b'\0'*4+data


def test_unrelated_slope_does_not_disqualify_selected_flat_tile():
    assert flat_height_plates(terrain(),point=(1,1))==[
        {'index':0,'bounds':[0,0,2,2],'height':1}]


def test_legacy_whole_map_and_actual_slope_still_refuse():
    with pytest.raises(EditorError,match='horizontal'):flat_height_plates(terrain())
    with pytest.raises(EditorError,match='horizontal'):flat_height_plates(terrain(),point=(4,1))
    assert flat_height_plates(terrain(),point=(2,1))==[]
